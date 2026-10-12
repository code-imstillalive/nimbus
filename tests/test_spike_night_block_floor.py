"""On a night with a forecast price spike inside a P2P block, the block
delivers only the expected matched P2P volume plus 10%, so the rest of the
battery is free for the spike (household, 12 Oct 2026: "10% over matched...
the rest is up for grabs").

Reference household, 12 Oct 2026: an 11.5 kW block 17:00-24:00 (80.5 kWh),
~45.7 kWh matched, a $13-21/kWh spike at 18:00-19:30. The block delivered
80.5 kWh at 0.47-1.05 $/kWh outside the spike while the battery ran out
before it could sell 40 kW in every spike period.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import _solver_path  # noqa: F401
import numpy as np
import pytest
import solver_plan
import solver_shared

AEST = timezone(timedelta(hours=10))
BLOCK = {
    "solver_p2p_block_1_rate_kw": 11.5,
    "solver_p2p_block_1_start_hour": 17,
    "solver_p2p_block_1_end_hour": 24,
}
ARMED = {
    **BLOCK,
    "solver_price_spike_override_armed": True,
    "solver_price_spike_threshold": 0.85,
}
MATCHED = 45.68  # kWh, the reference household's recent matched volume


def _times(days=2):
    start = datetime(2026, 10, 12, 0, 0, tzinfo=AEST)
    return [start + timedelta(minutes=30 * i) for i in range(48 * days)]


def _prices(times, spike_days=(12,)):
    out = []
    for t in times:
        spike = t.day in spike_days and t.hour == 18
        out.append(15.0 if spike else 0.30)
    return out


def _floor(cfg, times, prices, matched=MATCHED):
    fixed = solver_shared.fetch_p2p_fixed_export_kw(cfg, times)
    return fixed, solver_plan.spike_night_block_floor(
        cfg, fixed, prices, times, matched
    )


def _at(times, values, day, hour, minute=0):
    for t, v in zip(times, values, strict=True):
        if t.day == day and t.hour == hour and t.minute == minute:
            return v
    raise AssertionError("no such period")


def test_a_spike_night_block_drops_to_matched_plus_ten_percent():
    times = _times()
    _, out = _floor(ARMED, times, _prices(times))
    expected = 11.5 * 1.1 * MATCHED / 80.5  # 7.18 kW
    for hour in (17, 18, 20, 23):
        assert _at(times, out, 12, hour) == pytest.approx(expected)
    assert expected == pytest.approx(7.18, abs=0.01)


def test_a_night_without_a_spike_keeps_its_rate():
    times = _times()
    _, out = _floor(ARMED, times, _prices(times, spike_days=(12,)))
    for hour in (17, 18, 20, 23):
        assert _at(times, out, 13, hour) == 11.5


def test_the_post_midnight_zero_pins_are_untouched():
    times = _times()
    fixed, out = _floor(ARMED, times, _prices(times))
    for t, before, after in zip(times, fixed, out, strict=True):
        if t.hour < 4:
            assert after == before == 0.0


@pytest.mark.parametrize(
    "cfg, matched",
    [
        ({**ARMED, "solver_price_spike_override_armed": False}, MATCHED),
        ({**ARMED, "solver_price_spike_threshold": 0.0}, MATCHED),
        (ARMED, 0.0),
        # Matched + 10% covers the whole block: nothing to free.
        (ARMED, 80.5),
    ],
    ids=["unarmed", "no_threshold", "no_matched_volume", "matched_covers_block"],
)
def test_otherwise_the_pins_are_unchanged(cfg, matched):
    times = _times()
    fixed, out = _floor(cfg, times, _prices(times), matched)
    assert np.array_equal(np.array(fixed), np.array(out), equal_nan=True)


def test_no_block_is_a_no_op():
    times = _times()
    cfg = {
        "solver_price_spike_override_armed": True,
        "solver_price_spike_threshold": 0.85,
    }
    assert (
        solver_plan.spike_night_block_floor(cfg, None, _prices(times), times, MATCHED)
        is None
    )


def test_two_blocks_each_keep_their_share():
    times = _times()
    cfg = {
        **ARMED,
        "solver_p2p_block_1_end_hour": 20,
        "solver_p2p_block_2_rate_kw": 5.0,
        "solver_p2p_block_2_start_hour": 20,
        "solver_p2p_block_2_end_hour": 24,
    }
    prices = _prices(times)
    for i, t in enumerate(times):
        if t.day == 12 and t.hour == 21:
            prices[i] = 15.0
    _, out = _floor(cfg, times, prices)
    committed = 11.5 * 3 + 5.0 * 4
    share = 1.1 * MATCHED / committed
    assert _at(times, out, 12, 18) == pytest.approx(11.5 * share)
    assert _at(times, out, 12, 21) == pytest.approx(5.0 * share)


def test_only_the_block_holding_the_spike_drops():
    times = _times()
    cfg = {
        **ARMED,
        "solver_p2p_block_1_end_hour": 20,
        "solver_p2p_block_2_rate_kw": 5.0,
        "solver_p2p_block_2_start_hour": 20,
        "solver_p2p_block_2_end_hour": 24,
    }
    _, out = _floor(cfg, times, _prices(times))  # spike at 18:00 only
    committed = 11.5 * 3 + 5.0 * 4
    assert _at(times, out, 12, 18) == pytest.approx(11.5 * 1.1 * MATCHED / committed)
    assert _at(times, out, 12, 21) == 5.0
