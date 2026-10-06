"""A solve in each P2P block's start minute, lead time included.

The phase-locked cron solves at :00:30, :05:30, ..., so it can never plan a
lead-time minute such as 16:59, and the price watcher fires only when a
watched price's state actually changes. Measured on the reference household
1-5 Oct 2026, read-only:

    day     first plan at block rate   setpoint at block rate
    1 Oct   16:59:10                   16:59:31
    2 Oct   16:59:17                   16:59:39
    3 Oct   17:00:10                   17:00:24
    4 Oct   17:00:17                   17:00:32
    5 Oct   17:00:09                   17:00:23

Nimbus, its version and the 1-minute lead time were unchanged throughout.
What changed, at 10:20 AEST on 3 Oct, was the household's LocalVolts writer
moving to the v2 API: it still pushes every 15 s, but an unchanged v2
price no longer changes the sensor's state, so solves fell from ~215 an
hour to ~36 and the 16:55-17:00 window had none. This trigger plans the
block's first minute whatever the price sensors do.

Callbacks are captured from the real async_setup_entry(), as in
test_init_cron_suppression.py.
"""

import asyncio
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_init_cron_suppression import _make_entry, _make_hass

import custom_components.nimbus_load as nimbus_init
from custom_components.nimbus_load import solver_runtime
from custom_components.nimbus_load.solver_shared import (
    fetch_p2p_fixed_export_kw,
    p2p_block_start_minutes,
)

BLOCK_17_24_LEAD_1 = {
    "solver_p2p_block_1_rate_kw": 11.5,
    "solver_p2p_block_1_start_hour": 17,
    "solver_p2p_block_1_end_hour": 24,
    "solver_p2p_block_lead_time_minutes": 1,
}


def _config_state(attrs, state="configured"):
    s = MagicMock()
    s.state = state
    s.attributes = attrs
    return s


def _setup_and_fire(entry_id, attrs, fire_at_utc, run_solve, *, state="configured"):
    entry = _make_entry(entry_id)
    hass = _make_hass()
    hass.states.get = MagicMock(
        side_effect=lambda eid: (
            _config_state(attrs, state)
            if eid == "sensor.nimbus_solver_config"
            else None
        )
    )
    captured: dict[str, object] = {}

    def _capture(_hass, callback, **kwargs):
        if "minute" not in kwargs:
            captured["callback"] = callback
            captured["kwargs"] = kwargs
        return MagicMock()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(nimbus_init.health, "install_log_buffer_handler")
        )
        stack.enter_context(
            patch.object(nimbus_init.services, "async_register_services")
        )
        stack.enter_context(
            patch.object(
                nimbus_init.frontend,
                "async_register_frontend",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_get_integration",
                new=AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch.object(nimbus_init.solver_runtime, "async_run_solve", run_solve)
        )
        recorded = stack.enter_context(
            patch.object(nimbus_init.solver_runtime, "record_solve_completed")
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_track_utc_time_change",
                new=MagicMock(side_effect=_capture),
            )
        )
        asyncio.run(nimbus_init.async_setup_entry(hass, entry))
        asyncio.run(captured["callback"](fire_at_utc))
    return captured, recorded


def _utc(h, m, s=5):
    # AEST is UTC+10 with no DST.
    return datetime(2026, 10, 6, (h - 10) % 24, m, s, tzinfo=UTC)


def test_start_minutes_include_the_lead_time():
    assert p2p_block_start_minutes(BLOCK_17_24_LEAD_1) == {16 * 60 + 59}


def test_start_minutes_without_a_lead_time_are_the_block_start():
    cfg = {**BLOCK_17_24_LEAD_1, "solver_p2p_block_lead_time_minutes": 0}
    assert p2p_block_start_minutes(cfg) == {17 * 60}


def test_every_configured_block_has_a_start_minute():
    cfg = {
        "solver_p2p_block_1_rate_kw": 14,
        "solver_p2p_block_1_start_hour": 14,
        "solver_p2p_block_1_end_hour": 17,
        "solver_p2p_block_2_rate_kw": 4,
        "solver_p2p_block_2_start_hour": 17,
        "solver_p2p_block_2_end_hour": 18,
        "solver_p2p_block_3_rate_kw": 0,  # not configured
        "solver_p2p_block_3_start_hour": 20,
        "solver_p2p_block_3_end_hour": 21,
    }
    assert p2p_block_start_minutes(cfg) == {14 * 60, 17 * 60}


def test_no_blocks_no_start_minutes():
    assert p2p_block_start_minutes({}) == frozenset()


def test_the_trigger_and_the_plan_agree_on_when_the_block_starts():
    """The same parser feeds both, so the solve lands in the first minute
    the plan pins to the block rate."""
    start = min(p2p_block_start_minutes(BLOCK_17_24_LEAD_1))
    grid = [_utc(16, 58, 0), _utc(16, 59, 0), _utc(17, 0, 0)]
    kw = fetch_p2p_fixed_export_kw(BLOCK_17_24_LEAD_1, grid)
    assert kw[1] == 11.5 and kw[0] != kw[0]  # 16:59 in the block, 16:58 NaN
    assert start == 16 * 60 + 59


def test_registered_at_second_5_of_every_minute():
    captured, _ = _setup_and_fire(
        "entry_p2p_reg", BLOCK_17_24_LEAD_1, _utc(12, 0), AsyncMock(return_value=True)
    )
    assert captured["kwargs"] == {"second": nimbus_init._P2P_BLOCK_START_SECOND}
    assert nimbus_init._P2P_BLOCK_START_SECOND == 5


def test_solves_in_the_block_start_minute():
    run_solve = AsyncMock(return_value=True)
    _, recorded = _setup_and_fire(
        "entry_p2p_fire", BLOCK_17_24_LEAD_1, _utc(16, 59), run_solve
    )
    run_solve.assert_called_once()
    recorded.assert_called_once_with(trigger_source="p2p_block_start")


def test_does_nothing_in_any_other_minute():
    for h, m in ((16, 58), (17, 0), (12, 0)):
        run_solve = AsyncMock(return_value=True)
        _setup_and_fire(f"entry_p2p_{h}{m}", BLOCK_17_24_LEAD_1, _utc(h, m), run_solve)
        run_solve.assert_not_called()


def test_does_nothing_before_the_solver_is_configured():
    run_solve = AsyncMock(return_value=True)
    _setup_and_fire(
        "entry_p2p_unconf",
        BLOCK_17_24_LEAD_1,
        _utc(16, 59),
        run_solve,
        state="not_configured",
    )
    run_solve.assert_not_called()


def test_a_failed_solve_is_not_recorded():
    run_solve = AsyncMock(return_value=False)
    _, recorded = _setup_and_fire(
        "entry_p2p_failed", BLOCK_17_24_LEAD_1, _utc(16, 59), run_solve
    )
    run_solve.assert_called_once()
    recorded.assert_not_called()


def test_a_second_setup_cancels_the_first_trigger():
    entry = _make_entry("entry_p2p_twice")
    hass = _make_hass()
    unsubs = [MagicMock(name="p2p_1"), MagicMock(name="p2p_2")]
    it = iter(unsubs)

    def _track(_hass, _callback, **kwargs):
        return MagicMock() if "minute" in kwargs else next(it)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(nimbus_init.health, "install_log_buffer_handler")
        )
        stack.enter_context(
            patch.object(nimbus_init.services, "async_register_services")
        )
        stack.enter_context(
            patch.object(
                nimbus_init.frontend,
                "async_register_frontend",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_get_integration",
                new=AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init.solver_runtime,
                "async_run_solve",
                new=AsyncMock(return_value=True),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_track_utc_time_change",
                new=MagicMock(side_effect=_track),
            )
        )
        asyncio.run(nimbus_init.async_setup_entry(hass, entry))
        asyncio.run(nimbus_init.async_setup_entry(hass, entry))

    unsubs[0].assert_called_once()
    unsubs[1].assert_not_called()
    assert nimbus_init._p2p_start_timer_unsub["entry_p2p_twice"] is unsubs[1]
    solver_runtime._last_solve_completed_monotonic = None
