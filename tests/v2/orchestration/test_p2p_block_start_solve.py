"""A solve in each P2P block's start minute, lead time included (#1837)."""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from test_cron_suppression import _make_entry, _make_hass

import custom_components.nimbus_load as nimbus_init

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


def test_solves_in_the_block_start_minute():
    # test_solves_in_the_block_start_minute
    run_solve = AsyncMock(return_value=True)
    _, recorded = _setup_and_fire(
        "entry_p2p_fire", BLOCK_17_24_LEAD_1, _utc(16, 59), run_solve
    )
    run_solve.assert_called_once()
    recorded.assert_called_once_with(trigger_source="p2p_block_start")

    # test_does_nothing_outside_a_block
    for h, m in ((16, 58), (12, 0), (0, 30)):
        run_solve = AsyncMock(return_value=True)
        _setup_and_fire(f"entry_p2p_{h}{m}", BLOCK_17_24_LEAD_1, _utc(h, m), run_solve)
        run_solve.assert_not_called()

    # test_a_failed_solve_is_not_recorded
    run_solve = AsyncMock(return_value=False)
    _, recorded = _setup_and_fire(
        "entry_p2p_failed", BLOCK_17_24_LEAD_1, _utc(16, 59), run_solve
    )
    run_solve.assert_called_once()
    recorded.assert_not_called()

    # test_in_a_block_it_re_plans_when_no_solve_is_recent
    for s in (5, 20, 35, 50):
        run_solve, recorded = _replan(_utc(18, 10, s), since=15.2)
        run_solve.assert_called_once()
        recorded.assert_called_once_with(trigger_source="p2p_block_replan")

    # test_in_a_block_it_skips_when_any_solve_is_recent
    # e.g. a household automation calling solve_now every 15 s
    run_solve, _ = _replan(_utc(18, 10, 20), since=9.0)
    run_solve.assert_not_called()


# --- nimbus #1678: re-plan every ~15 s while a block is active --------------


def _replan(fire_at, since):
    run_solve = AsyncMock(return_value=True)
    with patch.object(
        nimbus_init.solver_runtime, "time_since_last_solve", return_value=since
    ):
        _, recorded = _setup_and_fire(
            f"entry_replan_{fire_at.minute}_{fire_at.second}_{since}",
            BLOCK_17_24_LEAD_1,
            fire_at,
            run_solve,
        )
    return run_solve, recorded
