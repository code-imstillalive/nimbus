"""Where the coordinator's training history comes from: long-term statistics,
recorder, or a hybrid of the two (#257) (#1837).
"""

from __future__ import annotations

import asyncio
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

# dt_util is a bare MagicMock in _ha_stubs -- give as_local() and utcnow() real
# behaviour so datetime comparisons and monotonicity assertions work. Same
# targeted-monkeypatch technique HA's own core tests use (dt_util is one of
# the few utilities whose real behaviour is genuinely load-bearing at the
# unit level, not just wallpaper).
import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x  # datetimes stay tz-aware as-is
_ha_util.dt.utcnow = lambda: datetime.now(UTC)
# nimbus issue #372 (Mark Purcell, codebase review): real behaviour needed
# here too, not left as a bare MagicMock -- _async_fetch_lts_history()'s
# own fix converts a float epoch "start" (what HA core's real
# statistics_during_period() actually returns, confirmed live) via
# dt_util.utc_from_timestamp() before handing it to as_local() above. A
# MagicMock stand-in would silently produce a MagicMock "timestamp"
# instead of a real datetime, masking exactly the class of bug this fix
# addresses. Matches HA core's own real implementation
# (functools.partial(datetime.fromtimestamp, tz=UTC)).
_ha_util.dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, tz=UTC)

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import (
    CONF_HYBRID_RECENT_DAYS,
    CONF_TRAINING_SOURCE,
    TRAINING_SOURCE_HYBRID,
    TRAINING_SOURCE_LTS,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _make_bare_coordinator(options: dict) -> NimbusCoordinator:
    """Bare coordinator wired only for the fetch-history dispatch under
    test -- everything else (retrain task tracking, ML training,
    persistence) is deliberately absent since none of it is exercised
    here. Same __new__ shortcut as test_retrain_task.
    """
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()

    # The real coordinator awaits `get_instance(hass).async_add_executor_job(_fetch)`
    # -- both fetch paths do this identically. Route it to a passthrough so
    # the inner _fetch() runs synchronously with its real logic under test.
    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    executor = MagicMock()
    executor.async_add_executor_job = AsyncMock(side_effect=_run_executor)
    # coordinator_module.get_instance is the stub-injected MagicMock from
    # _ha_stubs.py -- point it at our real-behaviour executor.
    coordinator_module.get_instance = MagicMock(return_value=executor)

    coord.entry = MagicMock()
    coord.entry.options = options
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry-lts-hybrid"
    return coord


def _run(coro):
    # asyncio.run() creates a fresh event loop AND closes it on return --
    # the earlier `asyncio.new_event_loop().run_until_complete(coro)` form
    # leaked one event loop (plus its internal self-pipe socket) per call,
    # 8 times across this file, which pytest's unraisable-exception hook
    # only surfaces once garbage collection happens to run during some
    # LATER, unrelated test -- see test_retrain_task.py
    # for the same asyncio.run() idiom already established in this repo.
    return asyncio.run(coro)


class _FakeState:
    """Minimal stand-in for a recorder State row -- just the attributes the
    coordinator's own _fetch() reads.
    """

    def __init__(
        self,
        state: str,
        last_changed: datetime,
        unit: str | None = "kW",
    ) -> None:
        self.state = state
        self.last_changed = last_changed
        self.attributes = {"unit_of_measurement": unit} if unit else {}


def _install_recorder_history(events: list[tuple[datetime, float]]) -> None:
    """Point the get_significant_states stub at a real-shaped return dict."""
    states = [_FakeState(str(v), t) for t, v in events]
    coordinator_module.get_significant_states = MagicMock(
        return_value={"sensor.test_load": states}
    )


def _install_lts_rows(rows: list[dict]) -> None:
    """Point the statistics_during_period stub at a real-shaped return dict."""
    coordinator_module.statistics_during_period = MagicMock(
        return_value={"sensor.test_load": rows}
    )


# ---------------------------------------------------------------------------
# Dispatch: training_source=recorder is the unchanged legacy path
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatch: training_source=lts pulls from statistics_during_period only
# ---------------------------------------------------------------------------


def test_lts_source_drops_insane_means():
    # test_lts_source_drops_insane_means: Same MAX_SANE_POWER_KW guard as the recorder path: a bad Modbus
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    _install_lts_rows(
        [
            {"start": now - timedelta(hours=2), "mean": 2.0},
            {"start": now - timedelta(hours=1), "mean": 21_474_836.5},  # dropped
        ]
    )

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    coord.hass.states.get = MagicMock(return_value=None)
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(hours=24),
            now,
            convert_power=True,
        )
    )

    values = [v for _, v in result]
    assert values == [2.0], f"insane value must be dropped, got {values}"

    # test_recorder_source_drops_nan_states: nimbus issue #353 (Mark Purcell): float("nan") does not raise, so
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    _install_recorder_history([])  # unused directly -- states set below
    coordinator_module.get_significant_states = MagicMock(
        return_value={
            "sensor.test_load": [
                _FakeState("2.0", now - timedelta(hours=2)),
                _FakeState("nan", now - timedelta(hours=1)),  # gap marker
                _FakeState("3.0", now),
            ]
        }
    )

    coord = _make_bare_coordinator({})
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(hours=24),
            now,
            convert_power=True,
        )
    )

    # nimbus issue #1556: the TRAINING fetch now keeps the NaN as an
    # explicit gap marker (see coordinator._async_fetch_change_only_history),
    # so an idle change-only circuit can be held without guessing outages
    # from gap length. #353's property is that a NaN never becomes a
    # training row, and that still holds: train_model() resamples a NaN
    # marker to None (test_change_only_history pins it). The forecast
    # path's fetch still drops it outright, asserted below.
    values = [v for _, v in result]
    assert values[0] == 2.0 and values[2] == 3.0
    assert math.isnan(values[1]), f"NaN must be a gap marker, got {values}"

    forecast_path = _run(
        coord._async_fetch_recorder_history(
            "sensor.test_load",
            now - timedelta(hours=24),
            now,
            convert_power=True,
        )
    )
    assert [v for _, v in forecast_path] == [2.0, 3.0]

    # test_lts_source_drops_nan_means: Same NaN-poisoning risk as the recorder path (see its own test) --
    now = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    _install_lts_rows(
        [
            {"start": now - timedelta(hours=2), "mean": 1.0},
            {"start": now - timedelta(hours=1), "mean": float("nan")},  # dropped
            {"start": now, "mean": 2.0},
        ]
    )

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    coord.hass.states.get = MagicMock(return_value=None)
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load", now - timedelta(hours=24), now
        )
    )

    values = [v for _, v in result]
    assert values == [1.0, 2.0], f"NaN mean must be dropped, got {values}"

    # test_hybrid_source_degrades_to_recorder_when_recent_days_exceeds_window: If a user misconfigures CONF_HYBRID_RECENT_DAYS >= CONF_TRAIN_DAYS
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    recorder_events = [(now - timedelta(hours=h), 7.7) for h in range(24)]
    _install_recorder_history(recorder_events)
    lts_sentinel = [{"start": now, "mean": 999.0}]  # must never be read
    _install_lts_rows(lts_sentinel)

    coord = _make_bare_coordinator(
        {
            CONF_TRAINING_SOURCE: TRAINING_SOURCE_HYBRID,
            # recent >= (end - start) -- guard-rail path
            CONF_HYBRID_RECENT_DAYS: 30,
        }
    )
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(days=30),  # start
            now,  # end -- recent_start = end - 30d = start
            convert_power=True,
        )
    )

    # Only recorder rows came back; LTS was not called.
    assert len(result) == 24
    for _, v in result:
        assert v == 7.7
    assert not coordinator_module.statistics_during_period.called

    # test_unknown_source_falls_back_to_recorder: A future release adds a fourth training_source that this build
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    recorder_events = [(now - timedelta(hours=h), 4.4) for h in range(24)]
    _install_recorder_history(recorder_events)

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: "not_a_real_source"})
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(days=30),
            now,
            convert_power=True,
        )
    )

    assert len(result) == 24
    for _, v in result:
        assert v == 4.4


# ---------------------------------------------------------------------------
# Dispatch: training_source=hybrid concatenates older-LTS + recent-recorder
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatch: unknown source falls back to recorder (defensive)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Binary sensors always force recorder regardless of training_source
# ---------------------------------------------------------------------------
