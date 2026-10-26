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


def test_a_participant_becomes_its_own_battery(site, caplog, car):
    # test_a_participant_becomes_its_own_battery
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

    # test_a_live_charge_limit_and_a_shared_charger_carry_through
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

    # test_a_participant_that_cannot_be_built_is_skipped_with_a_warning
    caplog.clear()
    for change, reason in [
        ({"battery_participant_capacity_kwh": None}, "missing capacity_kwh"),
        ({"battery_participant_soc_sensor": None}, "missing capacity_kwh"),
        ({"battery_participant_name": "home"}, "reserved"),
    ]:
        caplog.clear()
        site.add("ev", {**EV, **change})
        site.state("sensor.ev_soc", "55.0")
        with caplog.at_level(logging.WARNING):
            assert eb.build_extra_batteries() == []
        assert reason in caplog.text

    # test_a_departure_binds_to_the_first_one_in_the_horizon
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

    # test_a_departure_needs_both_fields_and_says_so_once
    caplog.clear()
    periods = _periods(6, 6, tz=sw.LOCAL_TZ)
    with caplog.at_level(logging.WARNING):
        assert _departure(site, pct=None, periods=periods) == (None, None)
        assert _departure(site, pct=None, periods=periods) == (None, None)
    assert caplog.text.count("only one of") == 1

    # test_a_soc_outside_its_own_range_is_warned_once_per_excursion
    caplog.clear()
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

    # test_an_away_participant_outside_its_range_is_not_warned_about
    caplog.clear()
    site.add(
        "ev", {**EV, "battery_participant_available_entity": "binary_sensor.ev_home"}
    )
    site.state("binary_sensor.ev_home", "off")
    site.state("sensor.ev_soc", "5.0")
    with caplog.at_level(logging.WARNING):
        eb.build_extra_batteries()
    assert "configured floor/ceiling" not in caplog.text

    # test_an_unreadable_soc_holds_the_last_reading_never_the_floor
    caplog.clear()
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


# --- what says a participant is here (22 Oct 2026) ---------------------------------------------


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


def test_with_nothing_to_hold_the_car_sits_out(car, caplog, site, monkeypatch):
    # test_with_nothing_to_hold_the_car_sits_out
    caplog.clear()
    for soc in ["unavailable", None]:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert car(soc) == []
        assert soc is None or "leaving it out" in caplog.text

    # test_a_participant_with_no_power_limits_is_built_and_warned_about_once
    caplog.clear()
    with monkeypatch.context() as mp:
        mp.setattr(eb, "_IMMOBILE_PARTICIPANT_WARNED", set())
        stuck = {
            **EV,
            "battery_participant_max_charge_kw": 0.0,
            "battery_participant_max_discharge_kw": 0.0,
        }
        site.add("stuck", stuck)
        site.state("sensor.ev_soc", "55.0")
        for _ in range(2):
            with caplog.at_level(logging.WARNING):
                (b,) = eb.build_extra_batteries()  # still built: a warning, not a skip
        (line,) = [r.getMessage() for r in caplog.records if "#1067" in r.getMessage()]
        assert all(
            s in line
            for s in ("oracle", "j_star", "Set real", "remove the participant")
        )
        assert (b.max_charge_kw, b.max_discharge_kw) == (0.0, 0.0)

    # test_a_one_directional_participant_is_not_warned_about
    caplog.clear()
    with monkeypatch.context() as mp:
        for limits in [
            {"battery_participant_max_charge_kw": 0.0},  # discharge only: legitimate
            {"battery_participant_max_discharge_kw": 0.0},  # charge only: legitimate
        ]:
            caplog.clear()
            with mp.context() as mp:
                mp.setattr(eb, "_IMMOBILE_PARTICIPANT_WARNED", set())
                site.add("ev", {**EV, **limits})
                site.state("sensor.ev_soc", "55.0")
                with caplog.at_level(logging.WARNING):
                    assert len(eb.build_extra_batteries()) == 1
                assert "#1067" not in caplog.text


# --- a participant that can never move is built, and said once (#1067) ------------------------
