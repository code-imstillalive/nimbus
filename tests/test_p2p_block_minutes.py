"""P2P block start and end to the minute (household, 12 Oct 2026, asked for
before on #565, where only a shared lead time shipped). The blocks' From /
Until are time entities like the reserve's; the Solver still receives hours
as a float, so a block from 20:30 is 20.5 and covers 20:30, not 20:00.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import _solver_path  # noqa: F401
import pytest
import solver_shared

AEST = timezone(timedelta(hours=10))
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _cfg(start, end, rate=7.5):
    return {
        "solver_p2p_block_1_rate_kw": rate,
        "solver_p2p_block_1_start_hour": start,
        "solver_p2p_block_1_end_hour": end,
    }


def _times():
    t0 = datetime(2026, 10, 12, 0, 0, tzinfo=AEST)
    return [t0 + timedelta(minutes=5 * i) for i in range(12 * 24)]


def _pinned(cfg):
    times = _times()
    fixed = solver_shared.fetch_p2p_fixed_export_kw(cfg, times)
    return {t.strftime("%H:%M"): v for t, v in zip(times, fixed, strict=True)}


def test_a_block_starts_and_ends_on_the_minute():
    pins = _pinned(_cfg(20.5, 23.75))
    assert pins["20:25"] != pins["20:25"]  # NaN: not in the block
    assert pins["20:30"] == 7.5
    assert pins["23:40"] == 7.5
    assert pins["23:45"] != pins["23:45"]


def test_whole_hours_still_mean_what_they_did():
    pins = _pinned(_cfg(17, 24))
    assert pins["16:55"] != pins["16:55"]
    assert pins["17:00"] == 7.5
    assert pins["23:55"] == 7.5


def test_a_block_ending_at_midnight_shown_as_0000_still_runs_to_midnight():
    """A time picker shows 24:00 as 00:00, so the end reads 0."""
    assert repr(_pinned(_cfg(17, 0))) == repr(_pinned(_cfg(17, 24)))


def test_0000_to_0000_is_still_not_a_block():
    """The default 0 / 0 never configured a block; it must not become a
    whole-day block now that an end of 0 can mean midnight."""
    assert solver_shared._configured_p2p_blocks(_cfg(0, 0)) == []


def test_the_committed_energy_counts_minutes():
    assert solver_shared.p2p_blocks_daily_energy_kwh(_cfg(20.5, 24)) == pytest.approx(
        7.5 * 3.5
    )
    assert solver_shared.p2p_blocks_daily_energy_kwh(_cfg(17, 0)) == pytest.approx(
        7.5 * 7
    )


def test_the_lead_time_still_comes_off_a_minute_start():
    cfg = {**_cfg(20.5, 24), "solver_p2p_block_lead_time_minutes": 1}
    assert solver_shared._configured_p2p_blocks(cfg) == [(7.5, 20 * 60 + 29, 24 * 60)]


def test_the_block_times_are_time_entities_on_the_p2p_sub_device():
    time_src = (_PKG / "time.py").read_text(encoding="utf-8")
    for n in (1, 2, 3):
        assert f'CONF_SOLVER_P2P_BLOCK_{n}_START_HOUR, "P2P Block {n} From"' in time_src
        assert f'CONF_SOLVER_P2P_BLOCK_{n}_END_HOUR, "P2P Block {n} Until"' in time_src
    assert 'f"{entry.entry_id}_p2p"' in time_src
    number_src = (_PKG / "number.py").read_text(encoding="utf-8")
    assert "CONF_SOLVER_P2P_BLOCK_1_START_HOUR" not in number_src
    sensor_src = (_PKG / "sensor.py").read_text(encoding="utf-8")
    keys = sensor_src[sensor_src.index("_SOLVER_TIME_ENTITY_KEYS = (") :]
    keys = keys[: keys.index(")")]
    assert "CONF_SOLVER_P2P_BLOCK_3_END_HOUR" in keys
