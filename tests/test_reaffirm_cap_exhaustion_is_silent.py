"""nimbus issue #875 (review follow-up, 2026-09-16): once
`DEFAULT_MAX_REAFFIRMS_PER_DAY` is exhausted for a load whose command is
still diverging, `load_run_state.reaffirm_allowed()` correctly returns
False -- but `solver_writer.py`'s `apply_commanded_state_guard()` then
does NOTHING AT ALL. The `elif device_entity and load_run_state.
reaffirm_allowed(...)` branch (~solver_writer.py:13476) is simply not
entered, and there is no `else`. Compare the sibling activation-cap-hit
branch a few lines above (~13452-13459), which logs a WARNING every
single time it blocks a dispatch ("... wants ON but is capped at %s
activations/day -- not dispatched this cycle") -- a reaffirm-cap-hit
produces zero equivalent log signal. The only trace left behind is
`command_divergence_seconds()` quietly growing on a sensor attribute
nobody is prompted to go and check.

This is a RED test. It pins the CORRECT behaviour, not the current one:
once the daily reaffirm cap is hit while the device is still diverging,
Nimbus should log a WARNING naming the load and the fact that re-sends
have stopped for the day. It is expected to FAIL against the real code
as of this writing.

**Log-shape decision** (this task's own call, since no such log exists
today to copy): a single WARNING per (load, day) is the right shape --
not one every solve cycle. Once `reaffirms_today_day_key` matches
today's `day_key` and `reaffirms_today` has hit the cap, that condition
holds for every remaining solve of the day (the load is re-solved every
~5 minutes in production); logging on each of those would reproduce the
exact log-spam mistake this codebase has already had to clean up twice
elsewhere (v0.94.301/302's overlap-guard noise, and #773's diagnostic
being deliberately "quieted" to two-tier DEBUG/WARNING on 2026-09-15
once it was understood to be non-actionable every cycle). This file's
own `_FLOOR_CROSSING_WARNED`-style precedent already exists in
solver_writer.py (a per-`(subentry_id, day_key)` warned-once set for the
floor-crossing WARNING a few hundred lines above this one) -- the
correct fix almost certainly reuses that exact shape. The second test
below exercises two consecutive solve cycles past the cap and asserts
exactly one WARNING mentioning this load across both: not zero (the
bug), not two (the spam a naive per-cycle log would produce).

Setup follows this project's own established pattern for exercising
`apply_commanded_state_guard()` end-to-end (see
tests/test_solver_writer_controllable_loads.py's own
`TestDispatchCommandedState` / `_FakeRunStateStore` / `_make_running_loop`
-- duplicated here rather than imported, matching how every other
solver_writer test file in this repo keeps its own copy of these small
fakes). The one addition specific to this file: the load's prior
LoadRunState is SEEDED directly into the fake Store (already commanded
ON hours ago, already measured off just as long, already at
`reaffirms_today == DEFAULT_MAX_REAFFIRMS_PER_DAY`) rather than built up
solve-by-solve, since the interesting moment here is precisely "the cap
was already hit before this cycle even started."
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import _solver_path  # noqa: F401
import load_run_state
import numpy as np
import pytest
import solver_writer

_TZ = timezone(timedelta(hours=10))  # Australia/Brisbane, no DST


def _grid(start: datetime, n: int, minutes: int = 30) -> list[datetime]:
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


def _fake_subentry(subentry_id: str, subentry_type: str, data: dict):
    return SimpleNamespace(
        subentry_id=subentry_id, subentry_type=subentry_type, data=data
    )


def _fake_plan(sheddable=(), adequacy=()):
    return SimpleNamespace(
        sheddable_loads=list(sheddable), adequacy_loads=list(adequacy)
    )


def _fake_load_plan(subentry_id, kw_array):
    """A stand-in for SheddableLoadPlan -- only the field
    apply_commanded_state_guard() actually reads for a sheddable load."""
    return SimpleNamespace(subentry_id=subentry_id, served_kw=kw_array)


class _FakeRunStateStore:
    """Real (not mocked) in-memory stand-in for
    homeassistant.helpers.storage.Store, same shape/reasoning as
    tests/test_solver_writer_controllable_loads.py's own copy -- keyed by
    the literal `key` string so two instances built with the same key
    share data."""

    _shared_data: ClassVar[dict] = {}

    def __init__(self, hass, version: int, key: str) -> None:
        self._key = key
        self._shared_data.setdefault(key, None)

    async def async_load(self):
        return self._shared_data.get(self._key)

    async def async_save(self, data) -> None:
        self._shared_data[self._key] = data


_fake_storage_module = types.ModuleType("homeassistant.helpers.storage")
_fake_storage_module.Store = _FakeRunStateStore
sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)
sys.modules["homeassistant.helpers.storage"] = _fake_storage_module


def _make_running_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """A real, running background-thread event loop -- apply_commanded_
    state_guard() dispatches via asyncio.run_coroutine_threadsafe(), same
    reasoning as this project's own test_solver_writer_controllable_loads.py."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return loop, thread


class _FakeServiceCalls:
    """Stands in for hass.services (async_call(domain, service,
    service_data, blocking=...)); recorded as a plain tuple so a test can
    assert nothing was actually dispatched."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def async_call(self, domain, service, service_data, **kwargs):
        self.calls.append((domain, service, dict(service_data)))


_HUB_ENTRY_ID = "entry_reaffirm_cap"
_SUBENTRY_ID = "s_stuck_hws"
_DEVICE_ENTITY = "water_heater.stuck_hws"


class TestReaffirmCapExhaustionLogsAWarning(unittest.TestCase):
    """nimbus issue #875 review finding. Both tests here are expected to
    FAIL against the real code -- that failure IS the point: it pins the
    correct behaviour so a future fix has something concrete to turn
    green."""

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._loop, self._loop_thread = _make_running_loop()
        _FakeRunStateStore._shared_data.clear()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _hass(self):
        sub = _fake_subentry(
            _SUBENTRY_ID,
            "controllable_load",
            {"controllable_load_device_entity": _DEVICE_ENTITY},
        )
        entry = SimpleNamespace(
            entry_id=_HUB_ENTRY_ID, subentries={sub.subentry_id: sub}
        )
        services = _FakeServiceCalls()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            services=services,
            loop=self._loop,
            # apply_commanded_state_guard() reads .states via
            # _resolve_controllable_load_tuning() for every load,
            # regardless of kind -- same reasoning/shape as
            # TestDispatchCommandedState._hass() in
            # test_solver_writer_controllable_loads.py: no live tuning
            # number entities configured, a clean no-op.
            states=SimpleNamespace(get=lambda eid: None),
        )
        return hass, services

    def _seed_exhausted_diverging_state(
        self, now: datetime
    ) -> load_run_state.LoadRunState:
        """A load Nimbus commanded ON hours ago, whose device has been
        measured OFF ever since (the #875 SG-Ready-bridge scenario the
        module docstring cites), and which has already used up every one
        of today's reaffirm allowance."""
        now_ts = now.timestamp()
        day_key = now.strftime("%Y-%m-%d")
        state = load_run_state.LoadRunState(
            commanded_state=True,
            commanded_since=now_ts - 5 * 3600,
            currently_on=False,
            off_since=now_ts - 4 * 3600,
            day_key=day_key,
            reaffirms_today=load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
            reaffirms_today_day_key=day_key,
        )

        async def _seed():
            store = load_run_state.LoadRunStateStore(
                store=_FakeRunStateStore(
                    None, 1, f"nimbus_load_{_HUB_ENTRY_ID}_load_run_state"
                )
            )
            await store.async_write(_SUBENTRY_ID, state)

        asyncio.run(_seed())

        # Sanity-check the FIXTURE, not the bug under test: confirm the
        # pure function genuinely agrees this load is capped and still
        # diverging, so a failure below is about the missing log, never
        # about a badly-built starting state.
        self.assertFalse(
            load_run_state.reaffirm_allowed(state, now_ts=now_ts, day_key=day_key),
            "fixture is broken -- reaffirm_allowed() should already be "
            "False once the daily cap is used up",
        )
        self.assertIsNotNone(
            load_run_state.command_divergence_seconds(state, now_ts),
            "fixture is broken -- the device must still be genuinely "
            "diverging for this scenario (cap exhaustion WHILE broken) "
            "to mean anything",
        )
        return state

    def _solve(self, at: datetime, grid_times: list[datetime]) -> None:
        plan = _fake_plan(
            sheddable=[_fake_load_plan(_SUBENTRY_ID, np.array([1.5, 1.5]))]
        )
        solver_writer.apply_commanded_state_guard(plan, at, grid_times)

    @pytest.mark.xfail(
        reason=(
            "nimbus issue #998: reaffirm_allowed() returning False produces "
            "zero log signal once the daily cap is hit. strict=True so this "
            "flips to a loud XPASS failure the moment the fix lands."
        ),
        strict=True,
    )
    def test_a_warning_is_logged_once_the_reaffirm_cap_is_hit(self):
        hass, services = self._hass()
        solver_writer._NATIVE_HASS = hass
        now = datetime(2026, 9, 16, 10, 0, tzinfo=_TZ)
        self._seed_exhausted_diverging_state(now)
        grid_times = _grid(now, 4, minutes=5)

        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as logs:
            self._solve(now, grid_times)

        matches = [
            r.message
            for r in logs.records
            if _SUBENTRY_ID in r.message
            and ("reaffirm" in r.message.lower() or "re-send" in r.message.lower())
        ]
        self.assertTrue(
            matches,
            "no WARNING named the load and the fact that re-sends have "
            "stopped once the daily reaffirm cap was hit -- this is "
            "nimbus issue #875's own silent-cap-exhaustion gap: unlike "
            "the activation-cap-hit branch a few lines above it (which "
            "always logs a WARNING), reaffirm_allowed() returning False "
            "here currently produces zero log signal at all. Log "
            f"records seen: {[r.message for r in logs.records]}",
        )
        # The cap is a real backstop and must still block the physical
        # re-send -- only the SILENCE around that is the bug being
        # pinned here, not the blocking itself.
        self.assertEqual(services.calls, [])

    @pytest.mark.xfail(
        reason=(
            "nimbus issue #998: no WARNING fires at all yet once the daily "
            "cap is hit, so there is nothing to deduplicate. strict=True so "
            "this flips to a loud XPASS failure the moment the fix lands."
        ),
        strict=True,
    )
    def test_the_warning_is_not_repeated_every_single_solve_cycle(self):
        """See the module docstring's "Log-shape decision" -- once per
        (load, day) is the right amount. Two consecutive ~5-minute solve
        cycles past the cap, same day, must produce exactly ONE WARNING
        mentioning this load, not two."""
        hass, services = self._hass()
        solver_writer._NATIVE_HASS = hass
        now = datetime(2026, 9, 16, 10, 0, tzinfo=_TZ)
        self._seed_exhausted_diverging_state(now)
        grid_times = _grid(now, 4, minutes=5)

        with self.assertLogs(solver_writer._LOGGER, level="WARNING") as logs:
            self._solve(now, grid_times)
            self._solve(now + timedelta(minutes=5), grid_times)

        occurrences = [
            r.message
            for r in logs.records
            if _SUBENTRY_ID in r.message
            and ("reaffirm" in r.message.lower() or "re-send" in r.message.lower())
        ]
        self.assertEqual(
            len(occurrences),
            1,
            "expected exactly one cap-exhaustion WARNING across two solve "
            f"cycles on the same day, got {len(occurrences)}: {occurrences}",
        )
        self.assertEqual(services.calls, [])


if __name__ == "__main__":
    unittest.main()
