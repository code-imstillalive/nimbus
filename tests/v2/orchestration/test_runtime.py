"""solver_runtime: the in-HA solve loop around solver_writer.main() (#1837).

One cycle's every outcome (missing dependency, lock held, solved, not
configured yet, the sensor-platform startup race, any other failure, a slow
cycle), the dispatch dry-run record, the follow-up after a mid-cycle trigger,
unload's wait for an in-flight solve, the module reset and the runtime paths.
The solver itself is a stand-in here: these are the loop's own promises.
Covers what lived in #89, #294/#295, #315, #365, #945, #1296, #1324, #1466,
#1482, #1658 and the 22 Oct 2026 follow-up rerun.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import tempfile
import time
import types
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

# The repository root, for the package import; tests/ is already on the
# path (pyproject), and must not shadow the package's own module names.
sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs
from _module_time import ModuleTime

install_ha_stubs()

rt = importlib.import_module("custom_components.nimbus_load.solver_runtime")
PKG = "custom_components.nimbus_load"
DRY_SWITCH = "switch.nimbus_solver_dispatch_dry_run"
FORECAST = "sensor.nimbus_solver_battery_forecast"


class Clock:
    """time.monotonic as the module sees it, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Every test starts from a fresh module and a clock it controls."""
    clock = Clock()
    monkeypatch.setattr(rt, "time", ModuleTime(clock))
    for name in (
        "forecast_snapshot_store",
        "quality_history_store",
        "decision_inputs_store",
    ):
        stub = types.ModuleType(f"{PKG}.{name}")
        stub.reset_snapshot_cache = stub.reset_quality_history_cache = lambda: None
        stub.reset_decision_inputs_cache = lambda: None
        monkeypatch.setitem(sys.modules, f"{PKG}.{name}", stub)
        monkeypatch.setattr(sys.modules[PKG], name, stub, raising=False)
    rt.reset_module_state()
    monkeypatch.setattr(rt, "_cycles_started", 0)
    monkeypatch.setattr(rt, "_in_flight_future", None)
    # set_default_env_vars only ever adds variables; none may outlive a test.
    saved = dict(os.environ)
    yield clock
    rt.reset_module_state()
    os.environ.clear()
    os.environ.update(saved)


class FakeSW:
    """The solver_writer surface the loop touches, recording each call."""

    def __init__(self, *, lock=True, main=None, posted=None):
        self.lock = lock
        self._main = main
        self.posted = posted
        self.calls: list[str] = []
        self.pushed: list[tuple] = []

    def acquire_lock(self):
        self.calls.append("acquire")
        return self.lock() if callable(self.lock) else self.lock

    def release_lock(self):
        self.calls.append("release")

    def main(self):
        self.calls.append("main")
        if self._main is not None:
            self._main()

    def ha_post_state(self, entity_id, state, attrs):
        self.pushed.append((entity_id, state, attrs))


class FakeHass:
    def __init__(self, states=None, entries=(), config_dir="/config"):
        self._states = dict(states or {})
        self.jobs: list[tuple] = []
        self.services = SimpleNamespace(async_call=object())
        self.config = SimpleNamespace(
            config_dir=config_dir, path=lambda *p: os.path.join(config_dir, *p)
        )
        self._entries = list(entries)
        self.states = SimpleNamespace(get=self._states.get)
        self.config_entries = SimpleNamespace(
            async_entries=lambda domain: list(self._entries),
            async_get_entry=lambda eid: next(
                (e for e in self._entries if e.entry_id == eid), None
            ),
        )

    def add_job(self, *args):
        self.jobs.append(args)

    def async_add_executor_job(self, fn, *args):
        future = asyncio.get_running_loop().create_future()
        future.set_result(fn(*args))
        return future


def _state(state, **attributes):
    return SimpleNamespace(state=state, attributes=attributes)


def _ready(monkeypatch, sw):
    monkeypatch.setattr(rt, "_solver_writer", sw)


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


# --- one cycle -------------------------------------------------------------


def test_a_missing_dependency_fails_the_cycle_and_notifies_once(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, f"{PKG}.solver_writer", None)
    monkeypatch.delattr(sys.modules[PKG], "solver_writer", raising=False)
    hass = FakeHass()
    with caplog.at_level(logging.ERROR, logger=rt.__name__):
        assert rt._run_one_cycle(hass) is False
        assert rt._run_one_cycle(hass) is False
    assert len(_messages(caplog, logging.ERROR)) == 2
    assert len(hass.jobs) == 1
    _call, domain, service, data = hass.jobs[0]
    assert (domain, service) == ("persistent_notification", "create")
    assert data["notification_id"] == "nimbus_solver_import_error"
    assert "highspy" in data["message"]


def test_a_successful_cycle_solves_then_releases_the_lock(monkeypatch):
    sw = FakeSW()
    _ready(monkeypatch, sw)
    assert rt._run_one_cycle(FakeHass()) is True
    assert sw.calls == ["acquire", "main", "release"]
    assert rt._cycles_started == 1


def test_first_use_binds_the_solver_and_backgrounds_the_stochastic_shadow(monkeypatch):
    sw = types.ModuleType(f"{PKG}.solver_writer")
    bound = []
    sw.set_native_hass = bound.append
    plan = types.ModuleType(f"{PKG}.solver_plan")
    plan.STOCHASTIC_SHADOW_IN_BACKGROUND = False
    for name, mod in (("solver_writer", sw), ("solver_plan", plan)):
        monkeypatch.setitem(sys.modules, f"{PKG}.{name}", mod)
        monkeypatch.setattr(sys.modules[PKG], name, mod, raising=False)
    for var in [v for v in os.environ if v.startswith("NIMBUS_")]:
        monkeypatch.delenv(var)
    hass = FakeHass(config_dir=tempfile.mkdtemp())
    assert rt._ensure_ready(hass) is sw
    assert rt._ensure_ready(hass) is sw
    assert bound == [hass]
    assert plan.STOCHASTIC_SHADOW_IN_BACKGROUND is True
    assert os.environ["NIMBUS_SOLVER_PLAN_STATE_PATH"].startswith(
        hass.config.config_dir
    )


def test_a_held_lock_skips_quietly_once_then_warns_and_asks_for_a_rerun(
    monkeypatch, caplog
):
    sw = FakeSW(lock=False)
    _ready(monkeypatch, sw)
    with caplog.at_level(logging.DEBUG, logger=rt.__name__):
        assert rt._run_one_cycle(FakeHass()) is False
        assert _messages(caplog, logging.WARNING) == []
        assert rt._run_one_cycle(FakeHass()) is False
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "consecutive skips: 2" in warnings[0]
    assert "main" not in sw.calls and "release" not in sw.calls
    assert rt._take_rerun_request() is True
    assert rt._take_rerun_request() is False
    # 29 Oct 2026: a caller that finds a cycle running asks it for the
    # follow-up; with nothing running it is told to start one itself.
    # A held lock (another caller's cycle) is "running", whatever
    # _in_flight_future says; one that ran and released it is not.
    monkeypatch.setattr(rt, "_cycle_running", False)
    assert rt.request_follow_up_if_running() is False
    monkeypatch.setattr(rt, "_cycle_running", True)
    assert rt.request_follow_up_if_running() is True
    assert rt._take_rerun_request() is True
    # The flag is the lock: set while main() runs, clear once it is released.
    seen = []
    sw = FakeSW(main=lambda: seen.append(rt._cycle_running))
    _ready(monkeypatch, sw)
    monkeypatch.setattr(rt, "_cycle_running", False)
    rt._run_one_cycle(FakeHass())
    assert seen == [True] and rt._cycle_running is False


def test_every_25th_single_overlap_is_summarised(monkeypatch, caplog):
    held = iter([False, True] * 25)
    sw = FakeSW(lock=lambda: next(held))
    _ready(monkeypatch, sw)
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        for _ in range(24 * 2):
            rt._run_one_cycle(FakeHass())
        assert _messages(caplog, logging.WARNING) == []
        rt._run_one_cycle(FakeHass())
    (summary,) = _messages(caplog, logging.WARNING)
    assert "25 single-tick overlaps" in summary


def test_not_configured_warns_once_only_after_ten_minutes_then_notes_recovery(
    monkeypatch, caplog, clean
):
    state = {"fail": True}

    def main():
        if state["fail"]:
            raise RuntimeError("Solver settings not configured yet")

    _ready(monkeypatch, FakeSW(main=main))
    with caplog.at_level(logging.INFO, logger=rt.__name__):
        assert rt._run_one_cycle(FakeHass()) is False
        clean.now += rt._NOT_CONFIGURED_WARN_AFTER_S - 1
        rt._run_one_cycle(FakeHass())
        assert _messages(caplog, logging.WARNING) == []
        clean.now += 1
        rt._run_one_cycle(FakeHass())
        clean.now += 60
        rt._run_one_cycle(FakeHass())
        (warning,) = _messages(caplog, logging.WARNING)
        assert "persisted for 600 s across 3 consecutive cycles" in warning
        state["fail"] = False
        assert rt._run_one_cycle(FakeHass()) is True
    (info,) = _messages(caplog, logging.INFO)
    assert "after 4 not-configured cycles" in info
    # A fresh episode waits the full grace again.
    state["fail"] = True
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        clean.now += 1
        rt._run_one_cycle(FakeHass())
    assert _messages(caplog, logging.WARNING) == []


@pytest.mark.parametrize(
    ("url", "code", "level", "text"),
    [
        (
            "native://sensor.nimbus_solver_config",
            404,
            logging.WARNING,
            "not registered yet",
        ),
        (
            "native://sensor.nimbus_solver_config",
            500,
            logging.ERROR,
            "solve cycle failed",
        ),
        ("native://sensor.other", 404, logging.ERROR, "solve cycle failed"),
    ],
)
def test_only_the_config_sensor_startup_race_is_a_plain_warning(
    monkeypatch, caplog, url, code, level, text
):
    def main():
        raise urllib.error.HTTPError(url, code, "x", {}, None)

    sw = FakeSW(main=main)
    _ready(monkeypatch, sw)
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        assert rt._run_one_cycle(FakeHass()) is False
    (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == level and text in record.getMessage()
    assert sw.calls[-1] == "release"


def test_any_other_failure_is_logged_and_the_lock_still_released(monkeypatch, caplog):
    def main():
        raise ValueError("boom")

    sw = FakeSW(main=main)
    _ready(monkeypatch, sw)
    with caplog.at_level(logging.ERROR, logger=rt.__name__):
        assert rt._run_one_cycle(FakeHass()) is False
    assert caplog.records[-1].exc_info is not None
    assert sw.calls == ["acquire", "main", "release"]


@pytest.mark.parametrize(("seconds", "warns"), [(120.0, False), (120.5, True)])
def test_a_cycle_over_two_minutes_is_flagged(
    monkeypatch, caplog, clean, seconds, warns
):
    def main():
        clean.now += seconds

    _ready(monkeypatch, FakeSW(main=main))
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        rt._run_one_cycle(FakeHass())
    slow = [m for m in _messages(caplog, logging.WARNING) if "threshold" in m]
    assert bool(slow) is warns
    if warns:
        assert "cycle took 120.5s" in slow[0]


# --- dispatch dry-run --------------------------------------------------------

_PERIOD = {
    "battery_kw": -2.3456,
    "soc_pct": 55.0,
    "grid_import_kw": 3.0,
    "grid_export_kw": 0.0,
    "import_price": 0.12,
    "export_price": 0.05,
    "time": "2026-09-20T06:00:00+00:00",
    "dispatch_direction": "charge",
}


def test_the_dry_run_records_the_current_period_without_any_service_call(monkeypatch):
    sw = FakeSW()
    hass = FakeHass(
        {DRY_SWITCH: _state("on"), FORECAST: _state("ok", forecast=[_PERIOD])}
    )
    rt._log_dispatch_dry_run(hass, sw)
    ((entity, value, attrs),) = sw.pushed
    assert entity == "sensor.nimbus_solver_dispatch_dry_run"
    assert value == -2.346
    assert attrs["soc_pct"] == 55.0 and attrs["period_time"] == _PERIOD["time"]
    assert attrs["dry_run_enabled"] is True
    assert attrs["dispatch_direction"] == "charge"
    assert attrs["dispatch_source_a_label"] is None
    assert hass.jobs == []


def test_the_dry_run_prefers_the_plan_this_cycle_posted(monkeypatch):
    sw = FakeSW()
    sw.last_posted = lambda eid: ("ok", {"forecast": [dict(_PERIOD, battery_kw=1.0)]})
    hass = FakeHass(
        {DRY_SWITCH: _state("on"), FORECAST: _state("ok", forecast=[_PERIOD])}
    )
    rt._log_dispatch_dry_run(hass, sw)
    assert sw.pushed[0][1] == 1.0


@pytest.mark.parametrize(
    ("states", "warning"),
    [
        ({DRY_SWITCH: _state("off")}, None),
        ({DRY_SWITCH: _state("on")}, None),
        ({DRY_SWITCH: _state("on"), FORECAST: _state("ok", forecast=[])}, "is empty"),
        (
            {
                DRY_SWITCH: _state("on"),
                FORECAST: _state("ok", forecast=[{"soc_pct": 1}]),
            },
            "no 'battery_kw' key (keys present: ['soc_pct'])",
        ),
    ],
    ids=["switch_off", "no_forecast_yet", "empty_forecast", "no_battery_kw"],
)
def test_the_dry_run_skips_without_a_usable_current_period(caplog, states, warning):
    sw = FakeSW()
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        rt._log_dispatch_dry_run(FakeHass(states), sw)
    assert sw.pushed == []
    found = _messages(caplog, logging.WARNING)
    assert (found == []) if warning is None else (warning in found[0])


def test_a_missing_entity_warns_once_per_absence_after_the_startup_grace(caplog, clean):
    sw = FakeSW()
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        rt._log_dispatch_dry_run(FakeHass(), sw)
        clean.now += rt._NOT_CONFIGURED_WARN_AFTER_S - 1
        rt._log_dispatch_dry_run(FakeHass(), sw)
        assert _messages(caplog, logging.WARNING) == []
        clean.now += 1
        rt._log_dispatch_dry_run(FakeHass(), sw)
        rt._log_dispatch_dry_run(FakeHass(), sw)
        assert len(_messages(caplog, logging.WARNING)) == 1
        # Present again ends the episode; a new absence waits the grace again.
        rt._log_dispatch_dry_run(FakeHass({DRY_SWITCH: _state("off")}), sw)
        rt._log_dispatch_dry_run(FakeHass(), sw)
        clean.now += rt._NOT_CONFIGURED_WARN_AFTER_S
        rt._log_dispatch_dry_run(FakeHass(), sw)
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 2
    assert all(DRY_SWITCH in w for w in warnings)


def test_a_missing_forecast_warns_after_the_grace_too(caplog, clean):
    with caplog.at_level(logging.WARNING, logger=rt.__name__):
        hass = FakeHass({DRY_SWITCH: _state("on")})
        rt._log_dispatch_dry_run(hass, FakeSW())
        clean.now += rt._NOT_CONFIGURED_WARN_AFTER_S
        rt._log_dispatch_dry_run(hass, FakeSW())
    (warning,) = _messages(caplog, logging.WARNING)
    assert FORECAST in warning


def test_a_failing_dry_run_never_fails_the_solve(monkeypatch, caplog):
    def main():
        pass

    sw = FakeSW(main=main)

    def broken(*_a):
        raise RuntimeError("post failed")

    sw.ha_post_state = broken
    hass = FakeHass(
        {DRY_SWITCH: _state("on"), FORECAST: _state("ok", forecast=[_PERIOD])}
    )
    _ready(monkeypatch, sw)
    with caplog.at_level(logging.ERROR, logger=rt.__name__):
        assert rt._run_one_cycle(hass) is True
    assert "logging failed, ignoring" in _messages(caplog, logging.ERROR)[0]


# --- the async wrapper -------------------------------------------------------


def _entry(entry_id="E1"):
    return SimpleNamespace(entry_id=entry_id)


def _stores(monkeypatch):
    """Stand-ins for the per-entry stores async_run_solve updates."""
    seen: list[tuple] = []

    def rec(name):
        async def f(hass, arg):
            seen.append((name, getattr(arg, "entry_id", arg)))

        return f

    for module, attrs in {
        "forecast_snapshot_store": (
            "async_ensure_snapshots_loaded",
            "async_capture_todays_snapshot",
        ),
        "quality_history_store": ("async_capture_quality_day",),
        "decision_inputs_store": ("async_capture",),
        "setup_health": ("async_note_solve_outcome",),
    }.items():
        mod = sys.modules.get(f"{PKG}.{module}")
        if mod is None or module == "setup_health":
            mod = types.ModuleType(f"{PKG}.{module}")
            monkeypatch.setitem(sys.modules, f"{PKG}.{module}", mod)
            monkeypatch.setattr(sys.modules[PKG], module, mod, raising=False)
        for attr in attrs:
            monkeypatch.setattr(mod, attr, rec(attr), raising=False)
    return seen


def test_a_successful_solve_records_the_outcome_and_captures_the_day(monkeypatch):
    seen = _stores(monkeypatch)
    _ready(monkeypatch, FakeSW())
    hass = FakeHass(entries=[_entry()])
    assert asyncio.run(rt.async_run_solve(hass)) is True
    assert seen == [
        ("async_ensure_snapshots_loaded", "E1"),
        ("async_note_solve_outcome", "E1"),
        ("async_capture_todays_snapshot", "E1"),
        ("async_capture_quality_day", "E1"),
        ("async_capture", "E1"),
    ]
    assert rt._in_flight_future is None


def test_a_failed_solve_records_the_outcome_but_captures_nothing(monkeypatch):
    seen = _stores(monkeypatch)

    def main():
        raise ValueError("x")

    _ready(monkeypatch, FakeSW(main=main))
    assert asyncio.run(rt.async_run_solve(FakeHass(entries=[_entry()]))) is False
    assert [s[0] for s in seen] == [
        "async_ensure_snapshots_loaded",
        "async_note_solve_outcome",
    ]


@pytest.mark.parametrize(
    "entries", [[], [_entry("A"), _entry("B")]], ids=["none", "two"]
)
def test_without_exactly_one_entry_nothing_per_entry_is_touched(monkeypatch, entries):
    seen = _stores(monkeypatch)
    _ready(monkeypatch, FakeSW())
    assert asyncio.run(rt.async_run_solve(FakeHass(entries=entries))) is True
    assert seen == []


def test_unreadable_entries_mean_no_snapshot_key():
    hass = FakeHass()

    def boom(_domain):
        raise RuntimeError("not loaded")

    hass.config_entries.async_entries = boom
    assert rt._entry_id_for_snapshot(hass) is None
    assert rt._entry_id_for_snapshot(FakeHass(entries=[_entry("X")])) == "X"


def test_a_trigger_during_a_cycle_gets_exactly_one_follow_up(monkeypatch):
    _stores(monkeypatch)
    sw = FakeSW()

    def main():
        rt._rerun_requested = True  # a tick arrived mid-cycle, every time

    sw._main = main
    _ready(monkeypatch, sw)
    assert asyncio.run(rt.async_run_solve(FakeHass())) is True
    assert sw.calls.count("main") == 2


def test_a_cycle_that_found_the_lock_held_runs_no_follow_up(monkeypatch):
    _stores(monkeypatch)
    sw = FakeSW(lock=False)
    _ready(monkeypatch, sw)
    assert asyncio.run(rt.async_run_solve(FakeHass())) is False
    assert sw.calls == ["acquire"]
    assert rt._take_rerun_request() is True  # left for the running cycle


def test_the_follow_up_counts_when_only_it_succeeds(monkeypatch):
    _stores(monkeypatch)
    runs = iter([ValueError("first"), None])

    def main():
        rt._rerun_requested = True
        err = next(runs)
        if err:
            raise err

    _ready(monkeypatch, FakeSW(main=main))
    assert asyncio.run(rt.async_run_solve(FakeHass())) is True


# --- unload --------------------------------------------------------------------


def test_unload_waits_for_nothing_when_no_solve_is_running():
    asyncio.run(rt.wait_for_in_flight_solve(timeout=0.01))


def test_unload_waits_for_the_running_solve_to_finish():
    async def go():
        future = asyncio.get_running_loop().create_future()
        rt._in_flight_future = future
        asyncio.get_running_loop().call_later(0.01, future.set_result, True)
        await rt.wait_for_in_flight_solve(timeout=5)
        return future.done()

    assert asyncio.run(go()) is True


@pytest.mark.parametrize(
    ("finish", "level", "text"),
    [
        (None, logging.WARNING, "still running 0s into unload"),
        ("cancel", logging.DEBUG, "cancelled before it started"),
        ("raise", logging.DEBUG, "own await raised"),
    ],
)
def test_unload_never_blocks_or_raises_on_a_stuck_or_failed_solve(
    caplog, finish, level, text
):
    async def go():
        future = asyncio.get_running_loop().create_future()
        if finish == "cancel":
            future.cancel()
        elif finish == "raise":
            future.set_exception(ValueError("solve died"))
        rt._in_flight_future = future
        await rt.wait_for_in_flight_solve(timeout=0.01)

    with caplog.at_level(logging.DEBUG, logger=rt.__name__):
        asyncio.run(go())
    assert any(r.levelno == level and text in r.getMessage() for r in caplog.records)


def test_reset_clears_every_cached_value_and_the_per_entry_stores(monkeypatch):
    cleared = []
    for name, attr in (
        ("forecast_snapshot_store", "reset_snapshot_cache"),
        ("quality_history_store", "reset_quality_history_cache"),
        ("decision_inputs_store", "reset_decision_inputs_cache"),
    ):
        monkeypatch.setattr(
            sys.modules[f"{PKG}.{name}"], attr, lambda n=name: cleared.append(n)
        )
    sensor = SimpleNamespace(record=lambda **kw: None)
    for attr, value in {
        "_solver_writer": object(),
        "_last_solve_completed_monotonic": 1.0,
        "_import_error_notified": True,
        "_price_latency_sensor": sensor,
        "_consecutive_lock_skips": 3,
        "_rerun_requested": True,
        "_consecutive_not_configured": 4,
        "_not_configured_since": 5.0,
        "_not_configured_warned": True,
        "_single_skip_total": 9,
    }.items():
        setattr(rt, attr, value)
    rt._STARTUP_ABSENCE_SINCE["k"] = 1.0
    rt._STARTUP_ABSENCE_WARNED.add("k")
    rt.reset_module_state()
    assert (rt._solver_writer, rt._last_solve_completed_monotonic) == (None, None)
    assert rt._import_error_notified is False and rt._price_latency_sensor is None
    assert (rt._consecutive_lock_skips, rt._single_skip_total) == (0, 0)
    assert rt._rerun_requested is False
    assert (rt._consecutive_not_configured, rt._not_configured_since) == (0, None)
    assert rt._not_configured_warned is False
    assert not rt._STARTUP_ABSENCE_SINCE and not rt._STARTUP_ABSENCE_WARNED
    assert cleared == [
        "forecast_snapshot_store",
        "quality_history_store",
        "decision_inputs_store",
    ]


# --- solve bookkeeping and paths ----------------------------------------------


def test_time_since_last_solve_counts_from_any_trigger(clean):
    assert rt.time_since_last_solve() is None
    rt.record_solve_completed(trigger_source="cron")
    clean.now += 42
    assert rt.time_since_last_solve() == 42


def test_only_a_price_change_solve_feeds_the_latency_sensor(monkeypatch):
    got = []
    rt.register_price_latency_sensor(
        SimpleNamespace(record=lambda **kw: got.append(kw))
    )
    solved = datetime(2026, 9, 20, 6, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(rt, "dt_util", SimpleNamespace(utcnow=lambda: solved))
    rt.record_solve_completed(trigger_source="cron")
    rt.record_solve_completed(trigger_source="startup")
    assert got == []
    changed = solved - timedelta(seconds=12)
    rt.record_solve_completed(
        trigger_source="price_change",
        triggering_entity="sensor.price",
        price_change_at=changed,
        debounce_s=5.0,
    )
    rt.record_solve_completed(trigger_source="price_change")
    assert got[0] == {
        "latency_s": 12.0,
        "trigger_source": "price_change",
        "triggering_entity": "sensor.price",
        "price_change_at": changed,
        "solve_at": solved,
        "debounce_s": 5.0,
    }
    assert got[1]["latency_s"] is None


def test_runtime_paths_persist_state_under_config_and_scaffolding_per_install(
    monkeypatch,
):
    for var in [v for v in os.environ if v.startswith("NIMBUS_")]:
        monkeypatch.delenv(var)
    rt.set_default_env_vars(FakeHass(config_dir="/srv/a"))
    first = {k: v for k, v in os.environ.items() if k.startswith("NIMBUS_")}
    for name in (
        "PLAN_STATE",
        "SOLAR_DELIVERY_RATIO",
        "AMBER_SPOT",
        "PRICE_CONSOLIDATION",
        "SOLAR_QUANTILES",
        "PRICE_ERROR_HISTORY",
    ):
        assert first[f"NIMBUS_SOLVER_{name}_PATH"].startswith(
            os.path.join("/srv/a", "")
        )
    assert first["NIMBUS_LP_DUMP_DIR"] == os.path.join("/srv/a", "nimbus_diagnostics")
    assert first["NIMBUS_SOLVER_PATH"] == os.path.dirname(rt.__file__)
    # #1324: the deleted-every-cycle files stay out of /config, keyed per install.
    tmp = tempfile.gettempdir()
    for var in ("NIMBUS_SOLVER_LOCK_PATH", "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH"):
        assert os.path.dirname(first[var]) == tmp
    for var in [v for v in os.environ if v.startswith("NIMBUS_")]:
        monkeypatch.delenv(var)
    rt.set_default_env_vars(FakeHass(config_dir="/srv/b"))
    assert os.environ["NIMBUS_SOLVER_LOCK_PATH"] != first["NIMBUS_SOLVER_LOCK_PATH"]
    # An operator's own setting wins.
    monkeypatch.setenv("NIMBUS_SOLVER_LOCK_PATH", "/mine.lock")
    rt.set_default_env_vars(FakeHass(config_dir="/srv/b"))
    assert os.environ["NIMBUS_SOLVER_LOCK_PATH"] == "/mine.lock"


def test_the_tests_clock_never_reaches_the_event_loop():
    """The clock is solver_runtime's own: asyncio reads the global
    time.monotonic, and frozen it never lets a timer fall due on Linux."""
    assert not isinstance(time.monotonic, Clock)
    assert isinstance(rt.time.monotonic(), float)
