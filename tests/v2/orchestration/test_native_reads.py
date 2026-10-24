"""The in-Home-Assistant half of the solver's reads and writes (#1837).

Inside HA the solver reads history through the recorder and publishes
through registered entity handlers, on a worker thread that hands work to
the event loop. Each test runs a real event loop on its own thread with a
recorder that answers from a dict, so the module's own thread hand-off,
filtering and fallbacks all run. Covers what lived in #85, #349, #1396,
#1570, #1643 and the native-mode files.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import types
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import solver_shared as shared
import solver_writer as sw
from golden.fake_ha import BASE, FakeHA, HistoryPoint

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
START, END = T0 - timedelta(hours=2), T0


class FakeHass:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.history: dict[str, list] = {}
        self.history_calls: list[tuple] = []
        self.history_error: Exception | None = None
        self.states_set: list[tuple] = []
        self.jobs: list = []
        self.service_result = None
        self.states = SimpleNamespace(
            get=lambda eid: None,
            async_set=lambda *a: self.states_set.append(a),
        )

        async def call(domain, service, data, blocking, return_response):
            if isinstance(self.service_result, Exception):
                raise self.service_result
            return self.service_result

        self.services = SimpleNamespace(async_call=call)

    def add_job(self, target, *args):
        self.jobs.append(target)
        target(*args)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()


@pytest.fixture
def hass(monkeypatch):
    h = FakeHass()

    def changes(hass_, start, end, entity_id, no_attributes):
        h.history_calls.append((start, end, entity_id, no_attributes))
        if h.history_error is not None:
            raise h.history_error
        return {entity_id: h.history.get(entity_id, [])}

    class Instance:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    history = types.ModuleType("homeassistant.components.recorder.history")
    history.state_changes_during_period = changes
    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda _hass: Instance()
    recorder.history = history
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.recorder.history", history
    )
    monkeypatch.setattr(shared.NATIVE, "hass", h)
    yield h
    h.close()


def _s(minutes, state, **attributes):
    return SimpleNamespace(
        state=state,
        last_changed=T0 + timedelta(minutes=minutes),
        attributes=attributes,
    )


# --- recorder reads ---------------------------------------------------------------


def test_history_comes_from_the_recorder_without_attributes(hass):
    hass.history["sensor.x"] = [_s(-10, "2"), _s(-30, "1"), _s(-20, "unknown")]
    got = sw.fetch_entity_history_range("sensor.x", START, END)
    assert got == [(T0 - timedelta(minutes=30), 1.0), (T0 - timedelta(minutes=10), 2.0)]
    assert got[0][0].tzinfo == sw.LOCAL_TZ
    assert hass.history_calls == [(START, END, "sensor.x", True)]


def test_an_attribute_history_needs_the_attributes_and_converts_each(hass):
    hass.history["sensor.x"] = [
        _s(-10, "1", rate="0.25"),
        _s(-20, "1", rate=None),
        _s(-30, "1", rate="n/a"),
        _s(-40, "1"),
        _s(-50, "1", rate="0.5"),
    ]
    got = sw.fetch_entity_attribute_history_range("sensor.x", "rate", START, END)
    assert [v for _t, v in got] == [0.5, 0.25]
    assert hass.history_calls[0][3] is False
    none = sw.fetch_entity_attribute_history_range(
        "sensor.x", "rate", START, END, convert=lambda raw: None
    )
    assert none == []


def test_power_history_from_the_recorder_is_in_kw(hass):
    hass.history["sensor.pv"] = [
        _s(-10, "3000", unit_of_measurement="W"),
        _s(-20, "2", unit_of_measurement="kW"),
        _s(-30, "5", unit_of_measurement="A"),
        _s(-40, "x", unit_of_measurement="W"),
    ]
    got = sw.fetch_entity_power_history_kw("sensor.pv", START, END)
    assert [v for _t, v in got] == [2.0, 3.0]


@pytest.mark.parametrize(
    ("states", "went"),
    [
        ([_s(-10, "1.0"), _s(-5, "2")], False),
        ([_s(-10, "1.0"), _s(-5, "unavailable")], True),
        ([_s(-90, "unavailable"), _s(-5, "2")], False),  # before `after`: not counted
        ([], False),
    ],
)
def test_went_unavailable_counts_only_changes_after_the_cutoff(hass, states, went):
    hass.history["sensor.x"] = states
    assert (
        shared.fetch_entity_went_unavailable("sensor.x", T0 - timedelta(hours=1), END)
        is went
    )


def test_a_failing_recorder_reads_as_no_history_not_an_error(hass):
    hass.history_error = RuntimeError("recorder busy")
    assert sw.fetch_entity_history_range("sensor.x", START, END) == []
    assert sw.fetch_entity_attribute_history_range("sensor.x", "a", START, END) == []
    assert sw.fetch_entity_power_history_kw("sensor.x", START, END) == []
    assert shared.fetch_entity_went_unavailable("sensor.x", START, END) is None


def test_went_unavailable_over_rest_too(monkeypatch):
    fake = FakeHA(states={})
    monkeypatch.setattr(sw, "HA_BASE", BASE)
    monkeypatch.setattr(sw, "_load_token", lambda: "test")
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    after = T0 - timedelta(hours=1)
    assert shared.fetch_entity_went_unavailable("sensor.x", after, END) is False
    fake.history["sensor.x"] = [
        HistoryPoint((T0 - timedelta(minutes=50)).isoformat(), "1"),
        HistoryPoint((T0 - timedelta(minutes=20)).isoformat(), "unavailable"),
    ]
    assert shared.fetch_entity_went_unavailable("sensor.x", after, END) is True
    assert (
        shared.fetch_entity_went_unavailable(
            "sensor.x", T0 - timedelta(minutes=10), END
        )
        is False
    )


# --- service calls with a response ------------------------------------------------


def test_a_service_response_comes_back_from_the_loop(hass):
    hass.service_result = {"calendar.x": {"events": []}}
    assert sw.ha_call_service_with_response("calendar", "get_events", {}) == {
        "calendar.x": {"events": []}
    }
    hass.service_result = RuntimeError("no such service")
    assert sw.ha_call_service_with_response("calendar", "get_events", {}) is None


# --- publishing -------------------------------------------------------------------


@pytest.fixture
def publish(hass, monkeypatch):
    monkeypatch.setattr(shared, "_ENTITY_UPDATE_HANDLERS", {})
    monkeypatch.setattr(shared, "_MANAGED_SKIP_STREAK", {})
    monkeypatch.setattr(shared, "_MANAGED_SKIP_WARNED", set())
    return hass


MANAGED = "sensor.nimbus_solver_battery_forecast"


def test_a_managed_entity_publishes_through_its_own_handler(publish):
    assert MANAGED in shared._NATIVE_MANAGED_ENTITY_IDS
    got = []
    shared._ENTITY_UPDATE_HANDLERS[MANAGED] = lambda state, attrs: got.append(
        (state, attrs)
    )
    assert shared.ha_post_state(MANAGED, 1.5, {"a": 1}) is True
    assert got == [(1.5, {"a": 1})]
    assert publish.states_set == []
    assert shared.last_posted(MANAGED) == (1.5, {"a": 1})
    assert shared.last_posted_at(MANAGED) is not None


def test_a_managed_entity_without_a_handler_is_skipped_and_warned_after_ten_cycles(
    publish, caplog
):
    with caplog.at_level(logging.INFO, logger=shared._LOGGER.name):
        for _ in range(shared._MANAGED_SKIP_WARN_AFTER - 1):
            assert shared.ha_post_state(MANAGED, 1, {}) is False
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
        shared.ha_post_state(MANAGED, 1, {})
        shared.ha_post_state(MANAGED, 1, {})
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1 and "10 consecutive solve cycles" in warnings[0]
        shared._ENTITY_UPDATE_HANDLERS[MANAGED] = lambda s, a: None
        shared.ha_post_state(MANAGED, 1, {})
    assert any(
        "publishing" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO
    )
    assert publish.states_set == [] and MANAGED not in shared._MANAGED_SKIP_STREAK


def test_an_unmanaged_entity_falls_back_to_a_raw_state_and_says_so(publish, caplog):
    with caplog.at_level(logging.WARNING, logger=shared._LOGGER.name):
        assert (
            shared.ha_post_state("sensor.nimbus_unregistered", "ok", {"k": 1}) is True
        )
    assert publish.states_set == [("sensor.nimbus_unregistered", "ok", {"k": 1})]
    assert any(
        "raw states.async_set fallback" in r.getMessage() for r in caplog.records
    )


def test_a_state_read_inside_home_assistant_carries_when_it_changed(hass):
    """The P2P rate merge (#1537) compares two sources by freshness, so the
    in-process read returns last_updated just as the REST path does."""
    state = types.SimpleNamespace(
        entity_id="sensor.p2p", state="0.3", attributes={"a": 1}, last_updated=T0
    )
    hass.states.get = lambda eid: state if eid == "sensor.p2p" else None
    got = sw.ha_get("sensor.p2p")
    assert got["last_updated"] == T0.isoformat() and got["attributes"] == {"a": 1}


def test_every_native_publish_leaves_a_trace_in_the_log(publish, caplog):
    """#85: the publish trace reaches the integration's own logger, where the
    household's log can show it, not only stdout."""
    with caplog.at_level(logging.DEBUG, logger=shared._LOGGER.name):
        shared.ha_post_state("sensor.nimbus_unregistered", "ok", {})
    assert any(
        r.levelno == logging.DEBUG
        and "sensor.nimbus_unregistered" in r.getMessage()
        and "via_handler=False" in r.getMessage()
        for r in caplog.records
    )
