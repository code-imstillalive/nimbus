"""From each battery-participant subentry to a battery the solver plans
(#467, #563, #1060, #1111) (#1837).

A participant's capacity, limits, efficiency and live SoC become its own
battery; one that cannot be built is skipped with a warning, never a
crash. While it is away it is gated for the next hour; a departure needs
both fields and binds to the first departure in the horizon; and a SoC
outside its own floor or ceiling is warned about once per excursion.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import availability
import numpy as np
import pytest
import solver_shared as shared
import solver_writer as sw
from solver.elements import MIN_CHARGE_DISCHARGE_COST_SPREAD, PeriodGrid
from solver_inputs import extra_batteries as eb

EV = {
    "battery_participant_name": "ev",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": "sensor.ev_soc",
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
}


@pytest.fixture
def site(monkeypatch):
    s = SimpleNamespace(states={}, subentries={})
    entry = SimpleNamespace(subentries=s.subentries)
    monkeypatch.setattr(
        shared.NATIVE,
        "hass",
        SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            states=SimpleNamespace(get=s.states.get),
        ),
    )
    monkeypatch.setattr(eb, "_BATTERY_PARTICIPANT_WARNED", set())

    def add(sid, data, kind="battery_participant"):
        s.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type=kind, data=data
        )

    def state(eid, value):
        s.states[eid] = SimpleNamespace(entity_id=eid, state=value, attributes={})

    s.add, s.state = add, state
    return s


def _periods(start_hour, n, hours=1.0, tz=UTC):
    return PeriodGrid(
        np.full(n, hours), start=datetime(2026, 9, 8, start_hour, tzinfo=tz)
    )


def test_a_participant_becomes_its_own_battery(site):
    site.add("ev", EV)
    site.add("pool", {"load_sensor": "sensor.pool"}, kind="load")
    site.state("sensor.ev_soc", "55.0")
    (b,) = eb.build_extra_batteries()
    assert (b.name, b.capacity_kwh, b.max_charge_kw, b.max_discharge_kw) == (
        "ev",
        60.0,
        11.0,
        11.0,
    )
    assert (b.min_soc_kwh, b.max_soc_kwh, b.initial_soc_kwh) == pytest.approx(
        (12.0, 57.0, 33.0)
    )
    assert b.charge_efficiency == pytest.approx(0.92**0.5) == b.discharge_efficiency
    assert b.charge_cost + b.discharge_cost >= MIN_CHARGE_DISCHARGE_COST_SPREAD
    assert b.available and b.shared_charger_group is None


def test_a_live_charge_limit_and_a_shared_charger_carry_through(site):
    site.add(
        "ev",
        {
            **EV,
            "battery_participant_charge_limit_entity": "number.ev_limit",
            "battery_participant_shared_charger_group": "dc",
            "battery_participant_shared_charger_max_kw": 25.0,
        },
    )
    site.state("sensor.ev_soc", "55.0")
    site.state("number.ev_limit", "80.0")
    (b,) = eb.build_extra_batteries()
    assert b.max_soc_kwh == pytest.approx(48.0)
    assert (b.shared_charger_group, b.shared_charger_max_kw) == ("dc", 25.0)


def test_a_glitch_reading_is_clamped_to_the_pack(site):
    site.add("ev", EV)
    site.state("sensor.ev_soc", "140.0")
    (b,) = eb.build_extra_batteries()
    assert b.initial_soc_kwh == b.capacity_kwh


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"battery_participant_capacity_kwh": None}, "missing capacity_kwh"),
        ({"battery_participant_soc_sensor": None}, "missing capacity_kwh"),
        ({"battery_participant_name": "home"}, "reserved"),
    ],
    ids=["no_capacity", "no_soc_sensor", "named_home"],
)
def test_a_participant_that_cannot_be_built_is_skipped_with_a_warning(
    site, caplog, change, reason
):
    site.add("ev", {**EV, **change})
    site.state("sensor.ev_soc", "55.0")
    with caplog.at_level(logging.WARNING):
        assert eb.build_extra_batteries() == []
    assert reason in caplog.text


def test_a_second_participant_with_the_same_name_is_skipped(site):
    site.add("a", EV)
    site.add("b", {**EV, "battery_participant_soc_sensor": "sensor.other"})
    site.add("c", {**EV, "battery_participant_name": "ev_2"})
    site.state("sensor.ev_soc", "55.0")
    site.state("sensor.other", "60.0")
    assert [b.name for b in eb.build_extra_batteries()] == ["ev", "ev_2"]


def test_outside_home_assistant_there_are_no_participants(monkeypatch):
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    assert eb.build_extra_batteries() == []


@pytest.mark.parametrize(
    ("home", "periods", "available", "gated_until"),
    [
        (None, _periods(6, 6), True, None),  # no presence entity: always home
        ("on", _periods(6, 6), True, None),
        ("off", _periods(6, 6), False, 1),  # gated for the next hour
        ("off", _periods(6, 10, 0.25), False, 4),
        ("off", None, False, None),  # no periods: the whole horizon
        ("missing", _periods(6, 6), False, 1),  # unknown is away
    ],
)
def test_an_away_participant_is_gated_for_the_next_hour(
    site, home, periods, available, gated_until
):
    data = dict(EV)
    if home is not None:
        data["battery_participant_available_entity"] = "binary_sensor.ev_home"
        if home != "missing":
            site.state("binary_sensor.ev_home", home)
    site.add("ev", data)
    site.state("sensor.ev_soc", "55.0")
    (b,) = eb.build_extra_batteries(periods)
    assert (b.available, b.unavailable_until_period_index) == (available, gated_until)


def _departure(site, *, hour=8, pct=90.0, periods=None):
    data = dict(EV)
    if hour is not None:
        data["battery_participant_departure_hour"] = hour
    if pct is not None:
        data["battery_participant_must_have_soc_by_departure_percent"] = pct
    site.add("ev", data)
    site.state("sensor.ev_soc", "55.0")
    (b,) = eb.build_extra_batteries(periods)
    return b.must_have_soc_by_period_index, b.must_have_soc_kwh


def test_a_departure_binds_to_the_first_one_in_the_horizon(site):
    local = sw.LOCAL_TZ
    assert _departure(site, periods=_periods(6, 6, tz=local)) == (
        2,
        pytest.approx(54.0),
    )
    # A 96-hour horizon from 18:00 holds four 07:00s; the first binds.
    four_days = _periods(18, 96, tz=local)
    idx, _kwh = _departure(site, hour=7, periods=four_days)
    assert idx == 13
    assert _departure(site, hour=20, periods=_periods(6, 6, tz=local)) == (None, None)
    assert _departure(site, periods=None) == (None, None)


def test_a_departure_needs_both_fields_and_says_so_once(site, caplog):
    periods = _periods(6, 6, tz=sw.LOCAL_TZ)
    with caplog.at_level(logging.WARNING):
        assert _departure(site, pct=None, periods=periods) == (None, None)
        assert _departure(site, pct=None, periods=periods) == (None, None)
    assert caplog.text.count("only one of") == 1


def test_a_soc_outside_its_own_range_is_warned_once_per_excursion(site, caplog):
    site.add("ev", EV)
    readings = ["98.0", "98.0", "50.0", "98.0"]  # out, still out, back, out again
    logged = []
    for value in readings:
        site.state("sensor.ev_soc", value)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            eb.build_extra_batteries()
        logged.append(
            {
                r.levelname
                for r in caplog.records
                if "configured floor/ceiling" in r.getMessage()
            }
        )
    assert logged == [{"WARNING"}, {"DEBUG"}, {"INFO"}, {"WARNING"}]


def test_an_away_participant_outside_its_range_is_not_warned_about(site, caplog):
    site.add(
        "ev", {**EV, "battery_participant_available_entity": "binary_sensor.ev_home"}
    )
    site.state("binary_sensor.ev_home", "off")
    site.state("sensor.ev_soc", "5.0")
    with caplog.at_level(logging.WARNING):
        eb.build_extra_batteries()
    assert "configured floor/ceiling" not in caplog.text


# --- what says a participant is here (22 Oct 2026) ---------------------------------------------


@pytest.mark.parametrize(
    ("entity", "here"),
    [
        ("binary_sensor.x", "on"),
        ("input_boolean.x", "on"),
        ("switch.x", "on"),
        ("device_tracker.car", "home"),
        ("person.driver", "home"),
    ],
)
def test_each_kind_keeps_its_standard_meaning(entity, here):
    states = availability.available_states(entity, None)
    assert availability.is_available(here, states)
    for not_here in ("off", "not_home", None, "unknown", "unavailable"):
        assert not availability.is_available(not_here, states)


def test_a_sensor_is_here_only_in_the_states_the_household_lists():
    listed = availability.available_states("sensor.charger", " Charging , Connected,")
    assert listed == {"charging", "connected"}
    assert availability.is_available("CHARGING", listed)
    assert not availability.is_available("Idle", listed)
    assert availability.available_states("sensor.charger", "") == frozenset()


@pytest.mark.parametrize(
    ("entity", "state", "listed", "available"),
    [
        ("binary_sensor.home", "on", None, True),
        ("binary_sensor.home", "off", None, False),
        ("device_tracker.car", "home", None, True),
        ("device_tracker.car", "not_home", None, False),
        ("person.driver", "home", None, True),
        ("person.driver", "Work", None, False),  # a zone is not home
        ("sensor.charger", "connected", "Charging, Connected", True),
        ("sensor.charger", "Disconnected", "Charging, Connected", False),
        ("sensor.charger", "Charging", None, False),  # none listed
    ],
)
def test_the_live_gate_reads_the_same_states(site, entity, state, listed, available):
    data = {**EV, "battery_participant_available_entity": entity}
    if listed is not None:
        data["battery_participant_available_states"] = listed
    site.add("ev", data)
    site.state("sensor.ev_soc", "55.0")
    site.state(entity, state)
    assert eb.build_extra_batteries()[0].available is available


# --- a sleeping car's unreadable SoC (#1715) ---------------------------------------------------


@pytest.fixture
def car(site, monkeypatch):
    monkeypatch.setattr(eb, "_LAST_GOOD_SOC", {})
    monkeypatch.setattr(eb, "_SOC_UNREADABLE_WARNED", set())
    site.add("car", EV)

    def build(soc):
        if soc is None:
            site.states.pop("sensor.ev_soc", None)
        else:
            site.state("sensor.ev_soc", soc)
        return eb.build_extra_batteries()

    return build


def _held(caplog):
    return [r.getMessage() for r in caplog.records if "#1715" in r.getMessage()]


def test_an_unreadable_soc_holds_the_last_reading_never_the_floor(car, caplog):
    (awake,) = car("70")
    with caplog.at_level(logging.WARNING):
        (asleep,) = car("unavailable")
    assert awake.initial_soc_kwh == pytest.approx(42.0) == asleep.initial_soc_kwh
    (line,) = _held(caplog)
    assert "holding" in line
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        car("unavailable")
    assert _held(caplog) == []  # once per spell
    car("65")
    assert "car" not in eb._SOC_UNREADABLE_WARNED  # waking ends the spell


@pytest.mark.parametrize("soc", ["unavailable", None], ids=["unavailable", "missing"])
def test_with_nothing_to_hold_the_car_sits_out(car, caplog, soc):
    with caplog.at_level(logging.WARNING):
        assert car(soc) == []
    assert soc is None or "leaving it out" in caplog.text


def test_the_hold_runs_out(car):
    car("70")
    pct, read_at = eb._LAST_GOOD_SOC["car"]
    eb._LAST_GOOD_SOC["car"] = (pct, read_at - eb._SOC_HOLD_SECONDS - 1)
    assert car("unavailable") == []
