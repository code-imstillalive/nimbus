"""Regression test for the LTS / hybrid training-source paths added to the
coordinator (nimbus repo issue #257).

The original coordinator training path read only the recorder's raw states
table, which is bounded by purge_keep_days (default 10, ~5.9 on Mark
Purcell's own install 2026-08-28). A configured CONF_TRAIN_DAYS=30 could
therefore silently return only 5.9 days of training data, blunting the
model against loads that vary on a longer weekly/seasonal timescale (see
issue #257 for the real live 3-4x under-forecast of midday load).

Long-term statistics -- hour-bucketed and kept indefinitely by the
recorder daemon -- is the source that survives the purge. The coordinator
now dispatches _async_fetch_training_history() to one of three paths per
the CONF_TRAINING_SOURCE option:

  - recorder (default): unchanged prior behaviour
  - lts:      hourly LTS only (via statistics_during_period)
  - hybrid:   recent recorder + older LTS, concatenated

This test proves all three dispatches work end-to-end against the real
NimbusCoordinator method, using stubbed recorder/statistics primitives
from _ha_stubs.py -- the same technique used by
test_coordinator_retrain_task_idempotent.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

# dt_util is a bare MagicMock in _ha_stubs -- give as_local() and utcnow() real
# behaviour so datetime comparisons and monotonicity assertions work. Same
# targeted-monkeypatch technique HA's own core tests use (dt_util is one of
# the few utilities whose real behaviour is genuinely load-bearing at the
# unit level, not just wallpaper).
import homeassistant.util as _ha_util  # noqa: E402

_ha_util.dt.as_local = lambda x: x  # datetimes stay tz-aware as-is
_ha_util.dt.utcnow = lambda: datetime.now(timezone.utc)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import (
    CONF_HYBRID_RECENT_DAYS,
    CONF_TRAINING_SOURCE,
    TRAINING_SOURCE_HYBRID,
    TRAINING_SOURCE_LTS,
    TRAINING_SOURCE_RECORDER,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _make_bare_coordinator(options: dict) -> NimbusCoordinator:
    """Bare coordinator wired only for the fetch-history dispatch under
    test -- everything else (retrain task tracking, ML training,
    persistence) is deliberately absent since none of it is exercised
    here. Same __new__ shortcut as test_coordinator_retrain_task_idempotent.
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
    return asyncio.new_event_loop().run_until_complete(coro)


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


def test_recorder_source_uses_only_recorder_history():
    """A stock install (no CONF_TRAINING_SOURCE set at all) must go
    through the recorder path exactly like it did before this PR, never
    touching LTS -- that's the whole point of the default resolving to
    'recorder' rather than silently changing behaviour on upgrade.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    recorder_events = [(now - timedelta(hours=h), 2.5) for h in range(24)]
    _install_recorder_history(recorder_events)
    lts_sentinel = [{"start": now, "mean": 999.0}]  # must never be read
    _install_lts_rows(lts_sentinel)

    # options=(no key set) -- default resolves to recorder
    coord = _make_bare_coordinator({})
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(days=30),
            now,
            convert_power=True,
        )
    )

    # 24 recorder events came back, no LTS sentinel value contaminates it.
    assert len(result) == 24, f"expected 24 rows, got {len(result)}"
    for _, v in result:
        assert v == 2.5, f"recorder value should pass through, got {v}"
    # And statistics_during_period was never actually called
    assert not coordinator_module.statistics_during_period.called


# ---------------------------------------------------------------------------
# Dispatch: training_source=lts pulls from statistics_during_period only
# ---------------------------------------------------------------------------


def test_lts_source_uses_only_statistics_during_period():
    """training_source='lts' should read only the LTS hourly aggregates,
    never fall back to recorder. Confirms the coordinator will actually
    train on 30/90/365 days of data on an install where recorder itself
    has already purged everything.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    # 30 days of hourly LTS rows -- the shape statistics_during_period
    # actually returns (mean is None for missing hours, filtered out).
    lts_rows = [
        {"start": now - timedelta(hours=h), "mean": 3.7}
        for h in range(30 * 24)
    ]
    _install_lts_rows(lts_rows)
    recorder_sentinel = [_FakeState("999.0", now)]
    coordinator_module.get_significant_states = MagicMock(
        return_value={"sensor.test_load": recorder_sentinel}
    )

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    # Wire the entity's current state so convert_power can read its unit.
    coord.hass.states.get = MagicMock(
        return_value=MagicMock(attributes={"unit_of_measurement": "kW"})
    )
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(days=30),
            now,
            convert_power=True,
        )
    )

    assert len(result) == 30 * 24, f"expected 720 rows, got {len(result)}"
    for _, v in result:
        assert v == 3.7, f"LTS value should pass through as kW, got {v}"
    # And get_significant_states was never actually called
    assert not coordinator_module.get_significant_states.called


def test_lts_source_drops_none_means():
    """statistics_during_period genuinely returns mean=None for a
    coverage gap (a sensor that briefly went unavailable, a fresh install
    where the recorder daemon hasn't rolled up the earliest hours yet).
    Dropping these mirrors the recorder path's own float() TypeError
    handling and keeps training set uncontaminated.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    _install_lts_rows(
        [
            {"start": now - timedelta(hours=3), "mean": 1.0},
            {"start": now - timedelta(hours=2), "mean": None},  # dropped
            {"start": now - timedelta(hours=1), "mean": 2.0},
        ]
    )

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    coord.hass.states.get = MagicMock(return_value=None)  # no unit read
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load", now - timedelta(hours=24), now
        )
    )

    values = [v for _, v in result]
    assert values == [1.0, 2.0], f"None-mean row must be dropped, got {values}"


def test_lts_source_drops_insane_means():
    """Same MAX_SANE_POWER_KW guard as the recorder path: a bad Modbus
    read (nimbus issue #-log 2026-08-17, a real 21_474_836.5 kW state)
    can propagate into an LTS row's mean. Drop it, don't train on it.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
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


# ---------------------------------------------------------------------------
# Dispatch: training_source=hybrid concatenates older-LTS + recent-recorder
# ---------------------------------------------------------------------------


def test_hybrid_source_concatenates_older_lts_and_recent_recorder():
    """hybrid should return older-first-then-recent -- LTS covering
    [start, recent_start) and recorder covering [recent_start, end].
    Both individually monotonic, LTS's last timestamp strictly less than
    recorder's first (statistics_during_period's end_time is exclusive by
    convention). ml/model.py's resample_last_value() relies on that
    monotonicity, so the concat MUST be older-then-recent.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    # 25 days of LTS rows (older half), 5 days of recorder (recent half).
    # With CONF_HYBRID_RECENT_DAYS=5 and 30-day train_days.
    lts_rows = [
        {"start": now - timedelta(days=5) - timedelta(hours=h), "mean": 1.0}
        for h in range(1, 25 * 24 + 1)
    ]
    lts_rows.sort(key=lambda r: r["start"])  # ascending
    _install_lts_rows(lts_rows)

    recorder_events = [
        (now - timedelta(hours=h), 2.0) for h in range(5 * 24)
    ]
    recorder_events.sort()  # ascending
    _install_recorder_history(recorder_events)

    coord = _make_bare_coordinator(
        {
            CONF_TRAINING_SOURCE: TRAINING_SOURCE_HYBRID,
            CONF_HYBRID_RECENT_DAYS: 5,
        }
    )
    coord.hass.states.get = MagicMock(
        return_value=MagicMock(attributes={"unit_of_measurement": "kW"})
    )
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(days=30),
            now,
            convert_power=True,
        )
    )

    # 25 days * 24h LTS + 5 days * 24h recorder = 720 rows total.
    assert len(result) == 25 * 24 + 5 * 24, f"got {len(result)}"

    # Everything before the recent_start boundary must be LTS (value 1.0);
    # everything after must be recorder (value 2.0).
    lts_count = sum(1 for _, v in result if v == 1.0)
    recorder_count = sum(1 for _, v in result if v == 2.0)
    assert lts_count == 25 * 24, f"LTS half got {lts_count}"
    assert recorder_count == 5 * 24, f"recorder half got {recorder_count}"

    # Timestamps must be monotonic non-decreasing across the whole result --
    # this is the ml/model.py resample_last_value() contract.
    timestamps = [t for t, _ in result]
    for a, b in zip(timestamps, timestamps[1:]):
        assert a <= b, f"non-monotonic at {a} -> {b}"

    # And the LTS half must strictly precede the recorder half at the boundary.
    lts_last = max(t for t, v in result if v == 1.0)
    recorder_first = min(t for t, v in result if v == 2.0)
    assert lts_last < recorder_first, (
        f"boundary overlap: LTS last {lts_last} vs recorder first {recorder_first}"
    )


def test_hybrid_source_degrades_to_recorder_when_recent_days_exceeds_window():
    """If a user misconfigures CONF_HYBRID_RECENT_DAYS >= CONF_TRAIN_DAYS
    (e.g. 30 recent days out of 30 total), the LTS slice would be empty
    and fetching it wastes an executor round-trip. Degrade to pure
    recorder rather than call statistics_during_period at all.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
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


# ---------------------------------------------------------------------------
# Dispatch: unknown source falls back to recorder (defensive)
# ---------------------------------------------------------------------------


def test_unknown_source_falls_back_to_recorder():
    """A future release adds a fourth training_source that this build
    doesn't understand yet, or an installer hand-edits an option to a
    typo. Either way, degrade to the recorder path with a WARNING log
    rather than crash the retrain -- same "never take the integration
    down mid-training" convention as ml/model.py's own returns-None-not-
    raises pattern (see train_model()'s own docstring).
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
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
# Binary sensors always force recorder regardless of training_source
# ---------------------------------------------------------------------------


def test_binary_sensor_always_uses_recorder_even_in_lts_mode():
    """A curtailment switch reports 'on'/'off' -- hourly LTS 'was it on
    27% of this hour?' isn't a state a lag-feature grid can consume. So
    binary=True must force the recorder path regardless of the
    training_source option, otherwise an LTS/hybrid install would
    silently lose its curtailment feature entirely.
    """
    now = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    # A real curtailment switch history: alternating on/off
    states = [
        _FakeState("on" if i % 2 == 0 else "off", now - timedelta(hours=i))
        for i in range(12)
    ]
    coordinator_module.get_significant_states = MagicMock(
        return_value={"sensor.test_load": states}
    )
    # LTS must not be called even though source=lts
    _install_lts_rows([{"start": now, "mean": 0.5}])

    coord = _make_bare_coordinator({CONF_TRAINING_SOURCE: TRAINING_SOURCE_LTS})
    result = _run(
        coord._async_fetch_training_history(
            "sensor.test_load",
            now - timedelta(hours=24),
            now,
            binary=True,
        )
    )

    # 12 recorder events came back with proper binary encoding.
    assert len(result) == 12
    values = [v for _, v in result]
    # Alternating 1.0/0.0 mirroring "on"/"off"
    assert values == [1.0, 0.0] * 6, f"got {values}"
    assert not coordinator_module.statistics_during_period.called


if __name__ == "__main__":
    # Run in-order so any earlier install_* mutation is deterministic --
    # the module namespace is shared across tests, same convention as the
    # other coordinator-level tests already do.
    tests = [
        test_recorder_source_uses_only_recorder_history,
        test_lts_source_uses_only_statistics_during_period,
        test_lts_source_drops_none_means,
        test_lts_source_drops_insane_means,
        test_hybrid_source_concatenates_older_lts_and_recent_recorder,
        test_hybrid_source_degrades_to_recorder_when_recent_days_exceeds_window,
        test_unknown_source_falls_back_to_recorder,
        test_binary_sensor_always_uses_recorder_even_in_lts_mode,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"PASS: {t.__name__}")
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
            raise
    print(f"\n{passed}/{len(tests)} tests passed")
