"""P2P block start and end to the minute: From / Until are time entities,
and the solver receives hours as a float, so a block from 20:30 is 20.5 and
covers 20:30, not 20:00 (#565) (#1837).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import solver_shared

AEST = timezone(timedelta(hours=10))
_PKG = Path(__file__).resolve().parents[3] / "custom_components" / "nimbus_load"


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


def test_a_block_ending_at_midnight_shown_as_0000_still_runs_to_midnight():
    """A time picker shows 24:00 as 00:00, so the end reads 0."""
    assert repr(_pinned(_cfg(17, 0))) == repr(_pinned(_cfg(17, 24)))
