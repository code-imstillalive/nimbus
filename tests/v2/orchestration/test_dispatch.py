"""From a plan's first period to a real device command (#476, #484, #534,
#595, #741, #756) (#1837).

Each solve's period-0 decision for a controllable load is debounced into a
commanded state, persisted, and dispatched as the device's own domain
expects -- only on a real change, never more than the daily cap allows,
and never on a decision that has not held for the load's minimum hold.
Driven through `apply_commanded_state_guard()` with a real event loop on
its own thread and an in-memory store; the device calls are recorded.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import load_run_state
import numpy as np
import pytest
import solver_shared as shared
import solver_writer as sw

TZ = timezone(timedelta(hours=10))
T0 = datetime(2026, 9, 11, 0, 0, tzinfo=TZ)
STEP = timedelta(minutes=5)
ON, OFF = np.array([1.5, 1.5]), np.array([0.0, 0.0])


class Store:
    data: ClassVar[dict] = {}

    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return self.data.get(self.key)

    async def async_save(self, data):
        self.data[self.key] = data


class Site:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.calls: list[tuple] = []
        self.failing: set[str] = set()
        self.loads: dict[str, dict] = {}

        async def call(domain, service, data, **kw):
            if domain in self.failing:
                raise RuntimeError(f"{domain} failed")
            self.calls.append((domain, service, dict(data)))

        entry = SimpleNamespace(entry_id="hub", subentries={})
        self._entry = entry
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            services=SimpleNamespace(async_call=call),
            states=SimpleNamespace(get=lambda eid: None),
            loop=self.loop,
        )

    def load(self, sid, device=None, **options):
        data = dict(options)
        if device:
            data["controllable_load_device_entity"] = device
        self._entry.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type="controllable_load", data=data
        )

    def solve(self, now, *, sheddable=None, adequacy=None, hours=None):
        plan = SimpleNamespace(
            sheddable_loads=[
                SimpleNamespace(subentry_id=s, served_kw=kw)
                for s, kw in (sheddable or {}).items()
            ],
            adequacy_loads=[
                SimpleNamespace(subentry_id=s, power_kw=kw, shortfall_kwh=0.0)
                for s, kw in (adequacy or {}).items()
            ],
        )
        grid = [now + STEP * i for i in range(6)]
        sw.apply_commanded_state_guard(plan, now, grid, hours)

    def state(self, sid):
        store = load_run_state.LoadRunStateStore(
            store=Store(None, 1, "nimbus_load_hub_load_run_state")
        )
        return asyncio.run(store.async_read(sid))

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()


@pytest.fixture
def site(monkeypatch):
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = Store
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    monkeypatch.setattr(Store, "data", {})
    s = Site()
    monkeypatch.setattr(shared.NATIVE, "hass", s.hass)
    # No power sensor on these loads: discovery is not what is tested here.
    monkeypatch.setattr(sw, "resolve_controllable_load_power_sensor", lambda d: None)
    yield s
    s.close()


@pytest.mark.parametrize(
    ("device", "options", "on_call", "off_call"),
    [
        ("switch.pump", {}, ("switch", "turn_on", {}), ("switch", "turn_off", {})),
        (
            "water_heater.hws",
            {},
            ("water_heater", "set_operation_mode", {"operation_mode": "performance"}),
            ("water_heater", "set_operation_mode", {"operation_mode": "eco"}),
        ),
        (
            "climate.ac",
            {"controllable_load_climate_on_hvac_mode": "cool"},
            ("climate", "set_hvac_mode", {"hvac_mode": "cool"}),
            ("climate", "set_hvac_mode", {"hvac_mode": "off"}),
        ),
    ],
    ids=["switch", "water_heater", "climate"],
)
def test_each_domain_is_commanded_its_own_way_and_off_needs_two_agreeing_solves(
    site, device, options, on_call, off_call
):
    site.load("s", device, controllable_load_min_hold_minutes=0, **options)
    site.solve(T0, sheddable={"s": ON})
    assert site.state("s").commanded_state is True
    assert site.calls == [(*on_call[:2], {"entity_id": device, **on_call[2]})]
    site.solve(T0 + STEP, adequacy={"s": OFF})
    assert len(site.calls) == 1  # one disagreeing solve is not yet a change
    site.solve(T0 + 2 * STEP, adequacy={"s": OFF})
    assert site.calls[-1] == (*off_call[:2], {"entity_id": device, **off_call[2]})
    assert site.state("s").commanded_state is False


def test_a_load_that_stays_on_is_commanded_once_and_its_first_decision_is_kept(site):
    site.load("s", "switch.pump")
    for i in range(5):
        site.solve(T0 + STEP * i, sheddable={"s": ON})
    assert len(site.calls) == 1
    st = site.state("s")
    assert st.commanded_state is True and st.commanded_since == T0.timestamp()


def test_alternating_decisions_change_the_command_at_most_once(site):
    site.load("s", "switch.pump")
    seen = []
    for i in range(10):
        site.solve(T0 + STEP * i, sheddable={"s": ON if i % 2 == 0 else OFF})
        seen.append(site.state("s").commanded_state)
    assert sum(a != b for a, b in itertools.pairwise(seen)) <= 1


def test_a_decision_that_flips_every_solve_never_reaches_the_device_until_it_holds(
    site,
):
    """#741: a heat pump stuck "about to start" all night. The decision
    alternated every 5-minute solve, so it never held for the 15-minute
    minimum and nothing was sent -- and once it settles, exactly one
    command goes out."""
    site.load("s", "water_heater.hws", controllable_load_min_hold_minutes=15)
    site.solve(T0, adequacy={"s": OFF})
    site.calls.clear()
    for i in range(24):
        site.solve(T0 + STEP * (i + 1), adequacy={"s": ON if i % 2 == 0 else OFF})
    assert site.calls == []
    assert site.state("s").commanded_state is False
    settle = T0 + STEP * 26
    for i in range(4):
        site.solve(settle + STEP * i, adequacy={"s": ON})
    assert site.calls == [
        (
            "water_heater",
            "set_operation_mode",
            {"entity_id": "water_heater.hws", "operation_mode": "performance"},
        )
    ]


def test_a_dip_inside_the_hold_window_does_not_turn_a_load_off_or_one_on_early(site):
    """#595: a jagged plan that dips for one period while the load is wanted
    again within the hold window keeps it on; the look-ahead never brings an
    off load on early."""
    site.load("on")
    site.load("off")
    hours = np.full(5, 5.0 / 60.0)
    later = np.array([0, 0, 0.65, 0.65, 0.65])
    site.solve(T0, adequacy={"on": np.full(5, 0.65), "off": later}, hours=hours)
    dip = np.array([0.0, 0.65, 0.0, 0.65, 0.0])
    site.solve(T0 + STEP, adequacy={"on": dip, "off": later}, hours=hours)
    assert site.state("on").commanded_state is True
    assert site.state("off").commanded_state is False


def test_the_daily_activation_cap_stops_a_second_start(site):
    site.load(
        "s",
        "switch.pump",
        controllable_load_max_activations_per_day=1,
        controllable_load_min_hold_minutes=0,
    )
    for i, kw in enumerate([ON, OFF, OFF, ON, ON]):
        site.solve(T0 + STEP * i, sheddable={"s": kw})
    assert [c[1] for c in site.calls].count("turn_on") == 1
    st = site.state("s")
    assert st.commanded_state is True and st.activations_today == 1


@pytest.mark.parametrize(
    ("device", "options", "warns"),
    [
        (None, {}, None),
        ("climate.ac", {}, None),  # no mode configured: never guessed
        ("fan.circulator", {}, "unsupported domain"),
    ],
)
def test_a_load_nimbus_cannot_command_is_still_decided_but_nothing_is_sent(
    site, caplog, device, options, warns
):
    site.load("s", device, **options)
    with caplog.at_level(logging.WARNING):
        site.solve(T0, sheddable={"s": ON})
    assert site.calls == []
    assert site.state("s").commanded_state is True
    if warns:
        assert warns in caplog.text


def test_a_failing_device_does_not_stop_the_others(site):
    site.load("bad", "switch.bad")
    site.load("good", "switch.good")
    site.failing = {"switch"}
    site.solve(T0, sheddable={"bad": ON, "good": ON})
    assert site.state("bad").commanded_state and site.state("good").commanded_state
