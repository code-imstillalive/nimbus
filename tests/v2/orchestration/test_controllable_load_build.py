"""From each controllable-load subentry to what the solver schedules
(#479, #486, #535, #626, #712, #713, #720) (#1837).

A sheddable load runs at its nominal power unless shed; a deferrable load
gets a target and a window, renewed each calendar day across the horizon,
cut by what it already delivered today, released once it reports done,
and pulled earlier when its tank would fall to its floor before the
window closes. Driven through `build_controllable_loads()` with the
subentries and live states a Home Assistant install would hold.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import ClassVar

import load_run_state
import pytest
import solver_shared as shared
import solver_writer as sw
from solver_inputs import controllable_loads as cl

TZ = timezone(timedelta(hours=10))
MIDNIGHT = datetime(2026, 9, 7, 0, 0, tzinfo=TZ)


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
        self.states: dict = {}
        self.entry = SimpleNamespace(entry_id="hub", subentries={})
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [self.entry]),
            states=SimpleNamespace(get=self.states.get),
            loop=self.loop,
        )

    def load(
        self, sid="s1", kind="deferrable", subentry_type="controllable_load", **data
    ):
        data = {"controllable_load_name": sid, "controllable_load_kind": kind, **data}
        self.entry.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type=subentry_type, data=data
        )

    def state(self, eid, value, **attrs):
        self.states[eid] = SimpleNamespace(entity_id=eid, state=value, attributes=attrs)

    def build(self, now=MIDNIGHT, n=48, minutes=30, grid=None):
        grid = grid or [now + timedelta(minutes=minutes * i) for i in range(n)]
        sheddable, adequacy, _thermal = sw.build_controllable_loads(
            now, grid, len(grid)
        )
        return sheddable, adequacy, grid

    def run_state(self, sid="s1"):
        store = load_run_state.LoadRunStateStore(
            store=Store(None, 1, "nimbus_load_hub_load_run_state")
        )
        return asyncio.run(store.async_read(sid))

    def seed(self, sid="s1", **fields):
        store = load_run_state.LoadRunStateStore(
            store=Store(None, 1, "nimbus_load_hub_load_run_state")
        )
        asyncio.run(store.async_write(sid, load_run_state.LoadRunState(**fields)))

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
    monkeypatch.setattr(sw, "_DONE_CONDITION_WARNED", set())
    monkeypatch.setattr(sw, "_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED", set())
    s = Site()
    monkeypatch.setattr(shared.NATIVE, "hass", s.hass)
    yield s
    s.close()


HWS = {"deferrable_max_power_kw": 3.7, "deferrable_target_kwh": 5.0}


def test_outside_home_assistant_there_is_nothing_to_build(monkeypatch):
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    grid = [MIDNIGHT + timedelta(minutes=30 * i) for i in range(4)]
    assert sw.build_controllable_loads(MIDNIGHT, grid, 4)[:2] == ([], [])


def test_each_kind_is_built_from_its_subentry_and_a_broken_one_is_skipped(site, caplog):
    site.load(
        "pump",
        "sheddable",
        sheddable_nominal_kw=1.5,
        sheddable_min_fraction=0.2,
        sheddable_shed_cost=3.0,
    )
    site.load(
        "hws",
        deferrable_max_power_kw=3.7,
        deferrable_target_kwh=5.0,
        deferrable_earliest_hour=1.0,
        deferrable_deadline_hour=6.0,
        deferrable_shortfall_price=8.0,
    )
    site.load("no_nominal", "sheddable")
    site.load("no_target", deferrable_max_power_kw=3.7)
    site.load("other", subentry_type="load", load_sensor="sensor.pool")
    with caplog.at_level(logging.WARNING):
        sheddable, adequacy, grid = site.build()
    (pump,) = sheddable
    assert (pump.name, pump.shed_cost, pump.min_fraction) == ("pump", 3.0, 0.2)
    assert len(pump.forecast_kw) == len(grid) and (pump.forecast_kw == 1.5).all()
    (hws,) = adequacy
    assert (hws.max_power_kw, hws.target_kwh, hws.shortfall_price) == (3.7, 5.0, 8.0)
    assert hws.value_per_kwh is None
    # 01:00 and 06:00 on a half-hour grid from midnight.
    assert (hws.earliest_period, hws.deadline_period) == (2, 12)
    assert [
        (w.earliest_period, w.deadline_period, w.target_kwh) for w in hws.windows
    ] == [(2, 12, 5.0)]
    assert "no_nominal" in caplog.text and "no_target" in caplog.text


@pytest.mark.parametrize(
    ("hour", "is_deadline", "expect"),
    [
        (2.0, False, 4),  # still ahead today
        (0.0, False, 47),  # already passed: tomorrow's, clamped to the horizon
        (6.0, True, 12),
        (6.25, True, 12),  # between periods: the one at or before
        (30.0, True, 47),
    ],
)
def test_an_hour_resolves_to_its_period_on_the_grid(hour, is_deadline, expect):
    grid = [MIDNIGHT + timedelta(minutes=30 * i) for i in range(48)]
    now = MIDNIGHT + timedelta(minutes=1)
    assert (
        cl._resolve_hour_to_period_index(grid, now, hour, is_deadline=is_deadline)
        == expect
    )


def test_a_window_open_now_starts_now_and_one_not_yet_open_starts_when_it_opens(site):
    site.load(**HWS, deferrable_earliest_hour=6.0, deferrable_deadline_hour=16.0)
    now = datetime(2026, 9, 9, 6, 1, tzinfo=TZ)
    grid, _ = sw.build_tiered_grid(now)
    _, (load,), _ = site.build(now, grid=grid)
    assert load.earliest_period == 0
    deadline = datetime(2026, 9, 9, 16, 0, tzinfo=TZ)
    assert grid[load.deadline_period] <= deadline < grid[load.deadline_period + 1]
    early = datetime(2026, 9, 9, 3, 0, tzinfo=TZ)
    grid, _ = sw.build_tiered_grid(early)
    _, (load,), _ = site.build(early, grid=grid)
    assert 0 < load.earliest_period and grid[load.earliest_period] <= early.replace(
        hour=6
    )
    assert len(load.windows) >= 3  # one per day of the horizon


def test_an_overnight_or_open_ended_window_is_one_window(site):
    site.load(
        "ev",
        deferrable_max_power_kw=7.0,
        deferrable_target_kwh=10.0,
        deferrable_earliest_hour=22.0,
        deferrable_deadline_hour=6.0,
    )
    site.load("open", **HWS, deferrable_earliest_hour=1.0)
    now = datetime(2026, 9, 9, 10, 0, tzinfo=TZ)
    grid, _ = sw.build_tiered_grid(now)
    _, (ev, open_ended), _ = site.build(now, grid=grid)
    assert ev.windows is None and ev.earliest_period < ev.deadline_period
    assert open_ended.windows is None and open_ended.deadline_period == len(grid) - 1


@pytest.mark.parametrize(
    ("static", "entity_state", "expect"),
    [
        (0.10, None, 0.10),
        (0.10, "0.25", 0.25),
        (0.10, "unavailable", 0.10),
        (None, "0.07", 0.07),
    ],
)
def test_a_live_value_per_kwh_overrides_the_static_one_while_it_reads(
    site, static, entity_state, expect
):
    data = {"deferrable_max_power_kw": 5.0, "deferrable_target_kwh": 0.5}
    if static is not None:
        data["deferrable_value_per_kwh"] = static
    if entity_state is not None:
        data["deferrable_value_per_kwh_entity"] = "input_number.pay"
        site.state("input_number.pay", entity_state)
    site.load(**data)
    _, (load,), _ = site.build(n=8)
    assert load.value_per_kwh == expect


@pytest.mark.parametrize(
    ("entity", "value", "done_when", "attrs", "released"),
    [
        ("binary_sensor.hot", "on", None, {}, True),
        ("binary_sensor.hot", "off", None, {}, False),
        ("binary_sensor.hot", "unavailable", None, {}, False),  # fails open
        ("sensor.tank", "65.0", ">= 60", {}, True),
        ("sensor.tank", "55.0", ">= 60", {}, False),
        ("sensor.tank", "hot", ">= 60", {}, False),
        (
            "water_heater.hws",
            "eco",
            None,
            {"current_temperature": 61, "temperature": 60},
            True,
        ),
        (
            "water_heater.hws",
            "eco",
            None,
            {"current_temperature": 59, "temperature": 60},
            False,
        ),
        ("water_heater.hws", "eco", ">= 58", {"current_temperature": 59}, True),
        ("water_heater.hws", "eco", ">= 58", {}, False),  # no reading: fails open
        ("water_heater.hws", "eco", None, {"current_temperature": 59}, False),
        ("climate.ac", "cool", ">= 20", {"current_temperature": 21}, True),
    ],
)
def test_a_load_that_reports_done_is_released_and_an_unreadable_one_is_scheduled(
    site, entity, value, done_when, attrs, released
):
    data = {**HWS, "deferrable_done_entity": entity}
    if done_when:
        data["deferrable_done_when"] = done_when
    site.load(**data)
    site.state(entity, value, **attrs)
    _, adequacy, _ = site.build(n=8)
    assert (adequacy == []) is released


@pytest.mark.parametrize(
    ("entity", "value", "attrs"),
    [
        ("sensor.tank", "55", {}),
        ("water_heater.hws", "eco", {"current_temperature": 55}),
    ],
)
def test_a_condition_that_cannot_be_read_is_warned_about_once(
    site, caplog, entity, value, attrs
):
    site.load(**HWS, deferrable_done_entity=entity, deferrable_done_when="> warm")
    site.state(entity, value, **attrs)
    with caplog.at_level(logging.WARNING):
        assert len(site.build(n=8)[1]) == 1  # not done: scheduled
        site.build(n=8)
    assert caplog.text.count("could not be evaluated") == 1


def _windows(now, delivered=0.0, done=False, grid=None):
    grid = grid or sw.build_tiered_grid(now)[0]
    return grid, cl._build_daily_adequacy_windows(
        grid, now, 6.0, 16.0, 2.0, today_delivered_kwh=delivered, today_done=done
    )


def test_each_day_of_the_horizon_gets_its_own_window_and_target():
    _grid, windows = _windows(datetime(2026, 9, 9, 3, 0, tzinfo=TZ))
    assert len(windows) == 4
    assert all(
        w.target_kwh == 2.0 and w.deadline_period >= w.earliest_period for w in windows
    )


def test_today_is_cut_by_what_was_delivered_and_dropped_once_met_done_or_closed():
    now = datetime(2026, 9, 9, 13, 5, tzinfo=TZ)
    _grid, partly = _windows(now, delivered=1.48)
    assert partly[0].target_kwh == pytest.approx(0.52)
    assert all(w.target_kwh == 2.0 for w in partly[1:])
    for kw in ({"delivered": 2.5}, {"done": True}):
        grid, windows = _windows(now, **kw)
        assert len(windows) >= 3
        assert all(grid[w.earliest_period].date() > now.date() for w in windows)
    late = datetime(2026, 9, 9, 20, 0, tzinfo=TZ)
    grid, windows = _windows(late)
    assert grid[windows[0].earliest_period].date() == late.date() + timedelta(days=1)


def test_a_window_running_past_the_horizon_is_clamped_not_dropped():
    now = datetime(2026, 9, 9, 3, 0, tzinfo=TZ)
    grid = [now + timedelta(hours=i) for i in range(8)]
    _grid, windows = _windows(now, grid=grid)
    assert [w.deadline_period for w in windows] == [7]


@pytest.mark.parametrize(
    ("temperature", "floor", "window"),
    [
        (50.0, 45.0, (12, 20)),  # crosses at 10:00: deadline pulled in
        (50.0, -5.0, (12, 32)),  # never crosses in the window
        (45.25, 45.0, (12, 12)),  # crosses before it opens: clamped, not inverted
        (44.0, 45.0, (12, 32)),  # already below: not this check's case
    ],
)
def test_a_tank_falling_to_its_floor_pulls_the_deadline_in(
    site, caplog, temperature, floor, window
):
    """#712/#713: the heat pump started itself, outside the plan, three
    nights running. Pulling the deadline in is what makes the plan heat
    before the floor; widening the start did nothing."""
    site.load(
        deferrable_max_power_kw=0.65,
        deferrable_target_kwh=4.0,
        deferrable_earliest_hour=6.0,
        deferrable_deadline_hour=16.0,
        deferrable_done_entity="water_heater.hws",
    )
    site.state(
        "water_heater.hws", "eco", current_temperature=temperature, min_temp=floor
    )
    with caplog.at_level(logging.WARNING):
        _, (load,), _ = site.build()
    first = load.windows[0]
    assert (first.earliest_period, first.deadline_period) == window
    assert first.target_kwh == 4.0
    assert ("physical floor" in caplog.text) is (window[1] < 32)


def test_a_floor_is_only_read_from_a_water_heater(site):
    site.load(
        **HWS,
        deferrable_earliest_hour=6.0,
        deferrable_deadline_hour=16.0,
        deferrable_done_entity="sensor.tank",
        deferrable_done_when=">= 60",
    )
    site.state("sensor.tank", "50.0", current_temperature=50.0, min_temp=45.0)
    _, (load,), _ = site.build()
    assert load.windows[0].deadline_period == 32


# --- what a load's power sensor says it delivered ------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "on"),
    [("1.5", None, True), ("1.5", "kW", True), ("4.6", "W", False), ("550", "W", True)],
)
def test_a_power_reading_is_read_in_kw_whatever_unit_the_sensor_reports(
    site, value, unit, on
):
    site.state("sensor.p", value, unit_of_measurement=unit)
    cl._sample_load_run_state("hub", "s1", "sensor.p", MIDNIGHT, "2026-09-07")
    assert site.run_state().currently_on is on


def test_delivered_energy_accrues_between_samples_and_the_watts_hint_is_logged_once(
    site, caplog
):
    site.state("sensor.p", "550", unit_of_measurement="W")
    with caplog.at_level(logging.INFO):
        for minutes in (0, 30):
            cl._sample_load_run_state(
                "hub", "s1", "sensor.p", MIDNIGHT + timedelta(minutes=minutes), "d"
            )
    st = site.run_state()
    assert st.currently_on and st.delivered_today_kwh == pytest.approx(0.275, abs=1e-3)
    assert caplog.text.count("reports Watts") == 1


@pytest.mark.parametrize("value", ["unavailable", None])
def test_a_reading_that_is_not_there_writes_nothing(site, value):
    if value:
        site.state("sensor.p", value)
    cl._sample_load_run_state("hub", "s1", "sensor.p", MIDNIGHT, "2026-09-07")
    assert site.run_state() == load_run_state.LoadRunState()


def test_building_samples_each_load_with_a_power_sensor(site):
    site.load(
        "s1",
        "sheddable",
        sheddable_nominal_kw=1.5,
        controllable_load_power_sensor="sensor.p",
    )
    site.state("sensor.p", "2.0")
    site.build(n=4)
    assert site.run_state().currently_on is True


@pytest.mark.parametrize(
    ("delivered", "day", "target"),
    [(1.48, "2026-09-07", 0.52), (2.165, "2026-09-07", None), (1.9, "2026-09-06", 2.0)],
    ids=["partly", "met", "yesterdays"],
)
def test_todays_delivery_cuts_the_target_and_yesterdays_does_not(
    site, delivered, day, target
):
    now = datetime(2026, 9, 7, 13, 5, tzinfo=TZ)
    site.seed(
        delivered_today_kwh=delivered, day_key=day, last_sample_at=now.timestamp()
    )
    site.load(
        controllable_load_power_sensor="sensor.p",
        deferrable_max_power_kw=0.65,
        deferrable_target_kwh=2.0,
    )
    site.state("sensor.p", "0.65")
    _, adequacy, _ = site.build(now, n=8)
    if target is None:
        assert adequacy == []
    else:
        assert adequacy[0].target_kwh == pytest.approx(target)


def test_outside_home_assistant_nothing_is_done_and_no_power_sensor_is_found(
    monkeypatch,
):
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    assert cl._evaluate_done_condition("binary_sensor.hot", None) is None
    assert sw._discover_power_sensor_for_device("switch.pump") is None
    monkeypatch.setattr(shared.NATIVE, "hass", SimpleNamespace())
    assert sw._discover_power_sensor_for_device("") is None
    # A cron run has no entity registry to read.
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", None)
    assert sw._discover_power_sensor_for_device("switch.pump") is None


def test_a_configured_power_sensor_always_wins_and_discovery_only_fills_a_gap(
    monkeypatch,
):
    asked = []
    monkeypatch.setattr(
        sw,
        "_discover_power_sensor_for_device",
        lambda device: asked.append(device) or "sensor.discovered",
    )
    configured = {
        "controllable_load_power_sensor": "sensor.mine",
        "controllable_load_device_entity": "switch.pump",
    }
    assert cl.resolve_controllable_load_power_sensor(configured) == "sensor.mine"
    assert asked == []
    unset = {"controllable_load_device_entity": "switch.pump"}
    assert cl.resolve_controllable_load_power_sensor(unset) == "sensor.discovered"
    assert asked == ["switch.pump"]
    assert cl.resolve_controllable_load_power_sensor({}) == "sensor.discovered"
    assert asked[-1] == ""
