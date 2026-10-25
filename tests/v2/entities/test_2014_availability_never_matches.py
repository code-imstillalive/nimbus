"""#2014: a Repair when a battery participant's "States that mean available"
never matches what its availability entity reports, so the participant reads
as away on every cycle and is never planned. Example from the issue: the
list says `Charging`, the charger reports `Charge`.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sh = importlib.import_module("custom_components.nimbus_load.setup_health")
av = importlib.import_module("custom_components.nimbus_load.availability")
const = importlib.import_module("custom_components.nimbus_load.const")

NOW = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


# --- the pure check ---------------------------------------------------------


def test_the_reported_case_is_unmatched():
    states = av.available_states("sensor.charger_status", "Charging, Connected")
    seen = ["Charge", "Idle", "unavailable", "Charge"]
    assert av.unmatched_states(seen, states) == ("Charge", "Idle")


def test_one_matching_state_in_the_window_is_enough():
    states = av.available_states("sensor.charger_status", "Charging")
    assert av.unmatched_states(["Idle", " charging "], states) is None


def test_no_real_state_reported_is_not_a_mismatch():
    states = av.available_states("sensor.charger_status", "Charging")
    assert av.unmatched_states([], states) is None
    assert av.unmatched_states(["unknown", "unavailable", None], states) is None


def test_which_entities_are_checked():
    # A list set: always checked.
    assert av.needs_listed_match("device_tracker.car", "home, charging")
    # A plain sensor with no list is never available: checked.
    assert av.needs_listed_match("sensor.charger_status", "")
    # A standard kind on its standard state: not checked, so a car simply
    # away all week ("not_home") raises nothing.
    assert not av.needs_listed_match("device_tracker.car", None)
    assert not av.needs_listed_match("binary_sensor.plugged_in", " , ")


# --- the Repair ---------------------------------------------------------------


def test_the_repair_names_the_participant_entity_states_and_list():
    issues = sh.evaluate_health(
        options={key: f"sensor.{key}" for key, _ in sh._REQUIRED_SOLVER_INPUTS},
        forecasts=(),
        hub_up_for=sh.TRAIN_GRACE,
        availability_mismatches=[
            ("sub1", "Tesla", "sensor.charger_status", ("Charge", "Idle"), "Charging")
        ],
    )
    (issue,) = issues
    assert issue.kind == sh.KIND_AVAILABILITY_MISMATCH
    assert issue.key == f"{sh.KIND_AVAILABILITY_MISMATCH}_sub1"
    assert issue.placeholders == {
        "name": "Tesla",
        "entity": "sensor.charger_status",
        "seen": "Charge, Idle",
        "listed": "Charging",
        "days": "7",
    }


def _entry(**participants):
    subs = {
        sid: SimpleNamespace(
            subentry_type=const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            title=sid,
            data=data,
        )
        for sid, data in participants.items()
    }
    subs["load1"] = SimpleNamespace(
        subentry_type=const.SUBENTRY_TYPE_LOAD, title="l", data={}
    )
    return SimpleNamespace(subentries=subs)


def _participant(entity, listed, name="Car"):
    return {
        const.CONF_BATTERY_PARTICIPANT_NAME: name,
        const.CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY: entity,
        const.CONF_BATTERY_PARTICIPANT_AVAILABLE_STATES: listed,
    }


def _run(entry, history, state=None, now=NOW):
    calls = []

    async def fake_seen(hass, entity_ids, start, end):
        calls.append((tuple(entity_ids), start, end))
        return history

    original = sh._async_states_seen
    sh._async_states_seen = fake_seen
    try:
        state = {} if state is None else state
        found = asyncio.run(sh._async_availability_mismatches(None, entry, state, now))
    finally:
        sh._async_states_seen = original
    return found, calls, state


def test_only_the_mismatched_participant_is_reported_over_a_week():
    entry = _entry(
        p1=_participant("sensor.charger_a", "Charging", name="Tesla"),
        p2=_participant("sensor.charger_b", "Connected", name="Mini"),
        p3=_participant("device_tracker.car", "", name="Away car"),
    )
    history = {
        "sensor.charger_a": ["Charge", "Idle"],
        "sensor.charger_b": ["Idle", "Connected"],
        "device_tracker.car": ["not_home"],
    }
    found, calls, _ = _run(entry, history)
    assert found == [
        ("p1", "Tesla", "sensor.charger_a", ("Charge", "Idle"), "Charging")
    ]
    # The tracker on its standard meaning is not even read.
    ((entity_ids, start, end),) = calls
    assert entity_ids == ("sensor.charger_a", "sensor.charger_b")
    assert end - start == timedelta(days=7)


def test_the_recorder_is_read_at_most_every_six_hours():
    entry = _entry(p1=_participant("sensor.charger_a", "Charging"))
    history = {"sensor.charger_a": ["Charge"]}
    found, calls, state = _run(entry, history)
    assert len(found) == 1 and len(calls) == 1
    again, calls2, _ = _run(entry, {}, state, NOW + timedelta(hours=1))
    assert again == found and calls2 == []
    later, calls3, _ = _run(
        entry, {"sensor.charger_a": ["Charging"]}, state, NOW + timedelta(hours=6)
    )
    assert later == [] and len(calls3) == 1


def test_a_failed_read_raises_no_repair():
    async def broken(*_a, **_k):
        raise RuntimeError("recorder not ready")

    original = sh._async_states_seen
    sh._async_states_seen = broken
    try:
        entry = _entry(p1=_participant("sensor.charger_a", "Charging"))
        assert (
            asyncio.run(sh._async_availability_mismatches(None, entry, {}, NOW)) == []
        )
    finally:
        sh._async_states_seen = original
