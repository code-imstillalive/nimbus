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
import thermal_forecast
from solver_dispatch import guard

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
        self.states: dict = {}

        async def call(domain, service, data, **kw):
            if domain in self.failing:
                raise RuntimeError(f"{domain} failed")
            self.calls.append((domain, service, dict(data)))

        entry = SimpleNamespace(entry_id="hub", subentries={})
        self._entry = entry
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            services=SimpleNamespace(async_call=call),
            states=SimpleNamespace(get=self.states.get),
            loop=self.loop,
        )

    def load(self, sid, device=None, **options):
        data = dict(options)
        if device:
            data["controllable_load_device_entity"] = device
        self._entry.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type="controllable_load", data=data
        )

    def solve(self, now, *, sheddable=None, adequacy=None, hours=None, step=STEP):
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
        grid = [now + step * i for i in range(6)]
        sw.apply_commanded_state_guard(plan, now, grid, hours)

    def state(self, sid):
        store = load_run_state.LoadRunStateStore(
            store=Store(None, 1, "nimbus_load_hub_load_run_state")
        )
        return asyncio.run(store.async_read(sid))

    def seed(self, sid, **fields):
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
    s = Site()
    monkeypatch.setattr(shared.NATIVE, "hass", s.hass)
    # A load's power sensor is the one it names: discovery is not tested here.
    monkeypatch.setattr(
        sw,
        "resolve_controllable_load_power_sensor",
        lambda d: d.get("controllable_load_power_sensor"),
    )
    # No recorder: the daily rate relearn falls back to its defaults.
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", None)
    monkeypatch.setattr(guard, "_FLOOR_CROSSING_WARNED", set())
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


# --- the water heater's own temperature forecast ------------------------------------------

HALF_HOUR = timedelta(minutes=30)
LEARNED = {
    "thermal_rates_learned_day_key": "2026-09-11",
    "thermal_heating_rate_c_per_kwh": 8.0,
    "thermal_idle_decay_c_per_hour": 0.5,
    "thermal_rates_source": "learned",
}


def _heater(site, live, *, seed=None, done_when=None, **attrs):
    data = {
        "deferrable_done_entity": "water_heater.hws",
        "controllable_load_power_sensor": "sensor.hws_power",
        "deferrable_max_power_kw": 3.7,
    }
    if done_when:
        data["deferrable_done_when"] = done_when
    site.load("hws", **data)
    site.seed("hws", **{**LEARNED, **(seed or {})})
    site.states["water_heater.hws"] = SimpleNamespace(
        state="eco", attributes={"current_temperature": live, **attrs}
    )


def _forecast(site, kw, *, step=HALF_HOUR, n=4):
    site.solve(
        T0 + timedelta(hours=8),
        adequacy={"hws": np.full(n, kw) if np.isscalar(kw) else kw},
        hours=np.full(n, step / timedelta(hours=1)),
        step=step,
    )
    return site.state("hws")


@pytest.mark.parametrize(
    ("seed", "live", "kw", "anchor", "first"),
    [
        ({}, 55.2, 0.0, 55.2, 55.2 - 0.25),  # settled: the reading is the anchor
        (
            {"currently_on": True, "last_idle_temperature": 50.0},
            40.0,
            3.7,
            50.0,
            50.0 + 14.8,
        ),
        (
            {
                "off_since": (T0 + timedelta(hours=8, minutes=-1)).timestamp(),
                "last_idle_temperature": 50.0,
            },
            39.0,
            0.0,
            50.0,
            49.75,
        ),  # stopped a minute ago: still settling
    ],
    ids=["settled", "heating", "settling"],
)
def test_a_reading_taken_while_heating_or_settling_never_becomes_the_anchor(
    site, seed, live, kw, anchor, first
):
    _heater(site, live, seed=seed)
    st = _forecast(site, kw)
    assert st.last_idle_temperature == anchor
    assert st.temperature_forecast[0]["value"] == pytest.approx(first, abs=0.01)


def test_during_a_hold_the_forecast_heats_from_the_first_period(site):
    """#595: the dip in the plan is not what the device does while held on."""
    since = (T0 + timedelta(hours=7, minutes=30)).timestamp()
    _heater(
        site,
        50.0,
        seed={
            "last_idle_temperature": 50.0,
            "commanded_state": True,
            "commanded_since": since,
        },
    )
    st = _forecast(site, np.array([0.0, 0.65, 0.0, 0.65, 0.0]), step=STEP, n=5)
    assert st.commanded_state is True
    assert st.temperature_forecast[0]["value"] == pytest.approx(
        50.0 + 3.7 * 8.0 / 12, abs=0.01
    )


@pytest.mark.parametrize(
    ("live", "attrs", "done_when", "first", "last"),
    [
        (58.0, {"temperature": 60.0}, None, 60.0, 60.0),  # its own setpoint
        (58.0, {}, None, 58.0 + 14.8, None),  # nothing to cap at
        (45.0, {"temperature": 45.0, "max_temp": 65.0}, None, 45.0 + 14.8, 65.0),
        (58.0, {}, ">= 60", 60.0, 60.0),  # the done condition's threshold
    ],
    ids=["setpoint", "none", "max_temp_over_eco", "done_when"],
)
def test_the_forecast_never_heats_past_the_tanks_ceiling(
    site, live, attrs, done_when, first, last
):
    _heater(site, live, done_when=done_when, **attrs)
    st = _forecast(site, 3.7)
    assert st.temperature_forecast[0]["value"] == pytest.approx(first, abs=0.01)
    if last is not None:
        assert st.temperature_forecast[-1]["value"] == last


@pytest.mark.parametrize(
    ("seed", "source", "rate"),
    [
        ({"thermal_rates_learned_day_key": ""}, "fallback", None),
        ({"thermal_rates_source": ""}, "fallback", None),  # stamped by an older learner
        ({"thermal_heating_rate_c_per_kwh": 1.2}, "learned", 1.2),
    ],
    ids=["never", "older_learner", "today"],
)
def test_the_rates_are_relearned_once_a_day_and_fall_back_without_a_recorder(
    site, seed, source, rate
):
    _heater(site, 55.0, seed=seed)
    st = _forecast(site, 0.0)
    assert st.thermal_rates_source == source
    assert st.thermal_rates_learned_day_key == "2026-09-11"
    expect = thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH if rate is None else rate
    assert st.thermal_heating_rate_c_per_kwh == expect


def test_a_recorder_that_fails_mid_fetch_falls_back_like_no_recorder(
    site, monkeypatch, caplog
):
    class Broken:
        async def async_add_executor_job(self, *_args):
            raise RuntimeError("recorder gone")

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda _hass: Broken()
    recorder.history = SimpleNamespace(state_changes_during_period=None)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)
    _heater(site, 55.0, seed={"thermal_rates_learned_day_key": ""})
    with caplog.at_level(logging.DEBUG, logger=shared._LOGGER.name):
        st = _forecast(site, 0.0)
    assert st.thermal_rates_source == "fallback"
    assert st.thermal_heating_rate_c_per_kwh == (
        thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
    )
    assert "thermal history fetch failed for water_heater.hws/sensor.hws_power" in (
        caplog.text
    )


def test_a_tank_forecast_to_reach_its_floor_says_when_and_warns_once_a_day(
    site, caplog
):
    _heater(site, 45.4, min_temp=45.0)
    with caplog.at_level(logging.WARNING):
        st = _forecast(site, 0.0)
        _forecast(site, 0.0)
    assert st.floor_crossing_forecast_time is not None
    assert st.floor_crossing_forecast_temperature <= 45.0
    assert caplog.text.count("hardware floor") == 1
    _heater(site, 55.0, min_temp=45.0)
    assert _forecast(site, 0.0).floor_crossing_forecast_time is None


# --- each load's own published plan ------------------------------------------------------------


def test_each_load_publishes_its_plan_and_what_it_costs(site):
    site.load("hws", deferrable_target_kwh=5.0)
    site.load("pump", sheddable_nominal_kw=1.5)
    hws = SimpleNamespace(
        subentry_id="hws",
        power_kw=np.array([2.0, 2.0, 0.0, 0.0]),
        shortfall_kwh=0.3,
        marginal_cost=21.891646007035034,
        profit_horizon=3.140159265358979,
    )
    pump = SimpleNamespace(subentry_id="pump", served_kw=np.array([1.5, 0.8, 1.5, 1.5]))
    plan = SimpleNamespace(sheddable_loads=[pump], adequacy_loads=[hws])
    grid = [T0 + HALF_HOUR * i for i in range(4)]
    sw.apply_commanded_state_guard(
        plan,
        T0,
        grid,
        np.full(4, 0.5),
        tariff_attributed_cost_by_subentry={"hws": 21.891646007035034},
    )
    st = site.state("hws")
    assert [p["value"] for p in st.plan_forecast] == [2.0, 2.0, 0.0, 0.0]
    assert st.plan_forecast[0]["time"] == grid[0].isoformat()
    assert [p["value"] for p in st.plan_delivered_kwh_forecast] == pytest.approx(
        [1.0, 2.0, 2.0, 2.0]
    )
    assert (st.plan_target_kwh, st.plan_shortfall_kwh) == (5.0, 0.3)
    assert (st.plan_marginal_cost, st.plan_profit_horizon) == (21.8916, 3.1402)
    assert st.plan_tariff_attributed_cost == 21.8916 and st.plan_nominal_kw is None
    shed = site.state("pump")
    assert shed.plan_forecast[1]["value"] == 0.8 and shed.plan_nominal_kw == 1.5
    assert shed.plan_target_kwh is None and shed.plan_tariff_attributed_cost is None


def test_the_plan_refreshes_every_solve_and_is_not_published_without_period_hours(site):
    site.load("pump", sheddable_nominal_kw=1.5)
    hours = np.full(4, 5 / 60)
    site.solve(T0, sheddable={"pump": np.full(4, 1.5)}, hours=hours)
    site.solve(
        T0 + STEP, sheddable={"pump": np.array([1.5, 0.9, 1.5, 1.5])}, hours=hours
    )
    st = site.state("pump")
    assert st.commanded_state is True and st.plan_forecast[1]["value"] == 0.9
    site.load("old", sheddable_nominal_kw=1.5)
    site.solve(T0, sheddable={"old": ON})
    assert site.state("old").plan_forecast is None


def test_a_plan_load_whose_subentry_is_gone_is_still_decided_but_not_sent(site):
    site.solve(T0, sheddable={"gone": ON})
    assert site.state("gone").commanded_state is True and site.calls == []


# --- a failure is reported, and costs no more than it must (#1019) ---------------------------


def test_a_load_that_fails_costs_only_itself_and_says_so(site, monkeypatch, caplog):
    site.load("bad", "switch.bad")
    site.load("good", "switch.good")
    real = sw._resolve_controllable_load_tuning

    def tuning(data, subentry):
        if subentry.subentry_id == "bad":
            raise RuntimeError("one load's config could not be read")
        return real(data, subentry)

    monkeypatch.setattr(sw, "_resolve_controllable_load_tuning", tuning)
    with caplog.at_level(logging.WARNING):
        site.solve(T0, sheddable={"bad": ON, "good": ON})
    assert [c[2]["entity_id"] for c in site.calls] == ["switch.good"]
    assert "'bad'" in caplog.text and "NOT commanded" in caplog.text


def test_a_failure_of_the_whole_cycle_is_a_warning_never_an_exception(site, caplog):
    def broken(domain):
        raise RuntimeError("config entries unavailable")

    site.hass.config_entries = SimpleNamespace(async_entries=broken)
    site.load("s", "switch.pump")
    with caplog.at_level(logging.WARNING):
        site.solve(T0, sheddable={"s": ON})  # does not raise
    assert site.calls == []
    assert "NOT commanded" in caplog.text


# --- a device that will not follow, once the day's re-sends are spent (#875) ------------------


def test_a_device_still_disobeying_after_the_days_resends_is_warned_about_once(
    site, monkeypatch, caplog
):
    """Commanded on five hours ago, measured off ever since, and today's
    re-send allowance used up: nothing more is sent, and the household is
    told once that something else may be writing to the device."""
    monkeypatch.setattr(guard, "_REAFFIRM_CAP_WARNED", set())
    site.load("hws", "water_heater.hws")
    now = T0 + timedelta(hours=10)
    day = now.strftime("%Y-%m-%d")
    site.seed(
        "hws",
        commanded_state=True,
        commanded_since=now.timestamp() - 5 * 3600,
        currently_on=False,
        off_since=now.timestamp() - 4 * 3600,
        day_key=day,
        reaffirms_today=load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
        reaffirms_today_day_key=day,
    )
    with caplog.at_level(logging.WARNING):
        site.solve(now, sheddable={"hws": ON})
        site.solve(now + STEP, sheddable={"hws": ON})
    assert site.calls == []
    warned = [r for r in caplog.records if "re-send cap" in r.getMessage()]
    assert len(warned) == 1 and "'hws'" in warned[0].getMessage()
