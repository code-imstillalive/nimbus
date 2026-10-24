"""A battery participant's day rebuilt from its own recorded history, so the
quality report scores the whole fleet, not just the home pack (#768, #585)
(#1837).

Each participant's power and SoC history become its achieved charge and
discharge. A reading is taken in its own recorded unit (#843), a reading
far outside the participant's own envelope is dropped rather than clamped
(#843), the hours it was away are zeroed (#467, #768), and the hours its
power sensor recorded nothing are zeroed and said so, never the last
reading held forward (#1161). One that cannot be built is left out; the
day is still scored. Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import load_run_state
import numpy as np
import pytest
import solver_writer as sw
from solver_inputs import battery_participants as bp

TZ = sw.LOCAL_TZ
DAY = datetime(2026, 8, 24, tzinfo=TZ)
END = DAY + timedelta(days=1)
GRID = [DAY + timedelta(hours=h) for h in range(24)]
EV = {
    "battery_participant_name": "ev",
    "battery_participant_capacity_kwh": 60.0,
    "battery_participant_soc_sensor": "sensor.ev_soc",
    "battery_participant_power_sensor": "sensor.ev_power",
    "battery_participant_power_positive_is_charge": True,
    "battery_participant_max_charge_kw": 11.0,
    "battery_participant_max_discharge_kw": 11.0,
    "battery_participant_min_soc_percent": 20.0,
    "battery_participant_max_soc_percent": 95.0,
    "battery_participant_efficiency_percent": 92.0,
}


def every(value, step=15, start=DAY, end=END):
    out, t = [], start
    while t < end:
        out.append((t, value(t) if callable(value) else value))
        t += timedelta(minutes=step)
    return out


@pytest.fixture
def site(monkeypatch):
    """One install: its subentries, live states, and recorded history."""
    s = SimpleNamespace(
        subentries={},
        states={"sensor.ev_soc": "55.0"},
        power=every(5.0),
        soc=every(55.0),
        presence=[],
    )
    entry = SimpleNamespace(subentries=s.subentries)
    monkeypatch.setattr(
        sw.NATIVE,
        "hass",
        SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda _d: [entry]),
            states=SimpleNamespace(
                get=lambda e: (
                    SimpleNamespace(entity_id=e, state=s.states[e], attributes={})
                    if e in s.states
                    else None
                )
            ),
        ),
    )
    monkeypatch.setattr(sw, "fetch_entity_power_history_kw", lambda *_a: s.power)
    monkeypatch.setattr(sw, "fetch_entity_history_range", lambda *_a: s.soc)
    monkeypatch.setattr(sw, "fetch_entity_state_history_range", lambda *_a: s.presence)

    def add(data=None, sid="s1", kind="battery_participant"):
        s.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type=kind, data=EV if data is None else data
        )

    s.add = add
    return s


def resolve():
    return bp._resolve_battery_participant_history(
        day_start=DAY, day_end=END, grid_times=GRID, period_hours=1.0, n_periods=24
    )


def test_a_participant_becomes_a_battery_with_its_day_read_as_charge(site):
    site.add()
    ((battery, charge, discharge, final_soc, soc_hist),) = resolve()
    assert (battery.name, battery.capacity_kwh) == ("ev", 60.0)
    assert battery.initial_soc_kwh == pytest.approx(33.0) == final_soc
    # positive is charge: a steady +5 kW is charging all day
    assert (charge == 5.0).all() and (discharge == 0.0).all()
    assert soc_hist == site.soc


def test_outside_home_assistant_there_is_no_fleet(site, monkeypatch):
    monkeypatch.setattr(sw.NATIVE, "hass", None)
    assert resolve() == []


@pytest.mark.parametrize(
    "case", ["none", "other_kind", "no_power_sensor", "no_history", "bad_soc_range"]
)
def test_a_participant_that_cannot_be_rebuilt_is_left_out_without_a_crash(site, case):
    if case == "other_kind":
        site.add({"load_sensor": "sensor.pool"}, kind="load")
    elif case == "no_power_sensor":
        site.add(
            {k: v for k, v in EV.items() if k != "battery_participant_power_sensor"}
        )
    elif case == "no_history":
        site.power = site.soc = []
        site.add()
    elif case == "bad_soc_range":
        site.add(
            {
                **EV,
                "battery_participant_min_soc_percent": 95.0,
                "battery_participant_max_soc_percent": 20.0,
            }
        )
    assert resolve() == []


# --- the hours it was away (#467, #768) -------------------------------------------------------

AWAY = range(8, 11)


@pytest.mark.parametrize(
    ("entity", "here", "gone"),
    [
        ("binary_sensor.ev_home", "on", "off"),
        ("device_tracker.ev", "home", "not_home"),
    ],
)
def test_driving_while_away_never_reaches_the_home_grid(site, entity, here, gone):
    site.add({**EV, "battery_participant_available_entity": entity})
    site.power = [
        (DAY + timedelta(hours=h), -8.0 if h in AWAY else 5.0) for h in range(24)
    ]
    site.presence = [
        (DAY + timedelta(hours=h), gone if h in AWAY else here) for h in range(24)
    ]
    ((battery, charge, discharge, _f, _s),) = resolve()
    for h in range(24):
        assert (charge[h], discharge[h]) == ((0.0, 0.0) if h in AWAY else (5.0, 0.0))
    assert battery.unavailable_period_indices == frozenset(AWAY) or set(
        battery.unavailable_period_indices
    ) == set(AWAY)


def test_no_presence_history_is_away_and_no_presence_entity_is_home(site):
    site.add({**EV, "battery_participant_available_entity": "binary_sensor.ev_home"})
    ((_b, charge, discharge, _f, _s),) = resolve()
    assert (charge == 0.0).all() and (discharge == 0.0).all()
    site.add(EV)
    ((battery, charge, _d, _f, _s),) = resolve()
    assert (charge == 5.0).all() and battery.unavailable_period_indices is None


# --- a power sensor that stopped recording (#1161) --------------------------------------------


def test_the_resample_holds_a_stale_power_reading_all_day():
    """The premise: the shared resample carries the last reading forward,
    so the participant path has to refuse it for power."""
    early = [(DAY + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
    assert sw.resample_history_mean(early, GRID, 1.0) == [5.0] * 24


def test_an_hour_with_no_reading_credits_nothing_and_is_named(site):
    site.add()
    site.power = [(DAY + timedelta(minutes=m), 5.0) for m in (0, 30, 60, 90, 120)]
    ((battery, charge, discharge, _f, _s),) = resolve()
    throughput = charge + discharge
    assert 0.0 < float(np.sum(throughput)) <= 20.0  # not 5 kW x 24 h
    assert list(throughput[4:]) == [0.0] * 20
    assert len(battery.stale_history_period_indices) > 15
    assert battery.unavailable_period_indices is None  # quiet is not away


@pytest.mark.parametrize("step", [15, 45], ids=["every_15_min", "every_45_min"])
def test_a_sensor_reporting_normally_loses_nothing(site, step):
    site.add()
    site.power = every(4.0, step=step)
    ((battery, charge, discharge, _f, _s),) = resolve()
    assert float(np.sum(charge + discharge)) == pytest.approx(96.0)
    assert battery.stale_history_period_indices is None


def test_the_gap_allowed_is_the_live_counters_own():
    only = [(DAY, 5.0)]
    assert load_run_state.MAX_SAMPLE_GAP_HOURS == 1.0
    assert 2 in sw._stale_power_period_indices(only, GRID, 1.0)
    assert 2 not in sw._stale_power_period_indices(only, GRID, 1.0, max_gap_hours=3.0)


@pytest.mark.parametrize(
    ("pts", "grid", "stale", "fresh"),
    [
        ([], [], set(), set()),
        ([], GRID, set(range(24)), set()),
        ([(DAY + timedelta(hours=12), 7.0)], GRID, set(range(12)), {12}),
        ([(DAY + timedelta(hours=3), 1.0), (DAY, 2.0)], GRID, set(), {0, 3}),
    ],
    ids=["no_grid", "no_samples", "before_the_first", "unsorted"],
)
def test_which_periods_are_unobserved(pts, grid, stale, fresh):
    out = sw._stale_power_period_indices(pts, grid, 1.0)
    assert stale <= out and not (fresh & out)
    if not grid:
        assert out == frozenset()


# --- each row in its own unit (#843 option A) -------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def rest(monkeypatch):
    seen = {}

    def run(rows, fn=sw.fetch_entity_power_history_kw, fail=False):
        def urlopen(req, *_a, **_k):
            if fail:
                raise sw.urllib.error.URLError("down")
            seen["url"] = req.full_url
            return _Resp([rows])

        monkeypatch.setattr(sw.NATIVE, "hass", None)
        monkeypatch.setattr(sw, "_load_token", lambda: "tok")
        monkeypatch.setattr(sw.urllib.request, "urlopen", urlopen)
        return [v for _t, v in fn("sensor.x", DAY, END)]

    run.seen = seen
    return run


def _row(hour, state, unit="kW"):
    row = {
        "state": str(state),
        "last_changed": (DAY + timedelta(hours=hour)).isoformat(),
    }
    if unit is not None:
        row["attributes"] = {"unit_of_measurement": unit}
    return row


def test_a_sensor_waking_in_watts_is_read_in_watts_row_by_row(rest):
    got = rest(
        [
            _row(8.851, 1514.417, "W"),
            _row(8.858, -774.818, "W"),
            _row(8.874, -0.774994),
            _row(8.875, -0.42282),
        ]
    )
    assert got == pytest.approx([1.514417, -0.774818, -0.774994, -0.42282])
    assert "minimal_response" not in rest.seen["url"]  # the units ride in attributes


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([_row(9, 7.5, None)], [7.5]),  # no unit: kW, as always
        ([_row(9, 7.5, "kWh"), _row(9.5, 7.5, "A"), _row(10, 2.0)], [2.0]),  # #1643
        ([_row(9, "unavailable", "W"), _row(10, "heating"), _row(11, 5.0)], [5.0]),
        ([_row(11, 3.0), _row(9, 1.0), _row(10, 2.0)], [1.0, 2.0, 3.0]),
        ([], []),
    ],
    ids=["no_unit", "not_power", "not_a_number", "unsorted", "empty"],
)
def test_what_each_row_reads_as(rest, rows, expected):
    assert rest(rows) == expected


def test_a_failed_read_is_no_history(rest):
    assert rest([], fail=True) == []


def test_every_other_history_read_stays_on_the_cheap_path(rest):
    rest([], fn=sw.fetch_entity_history_range)
    assert "minimal_response" in rest.seen["url"]


# --- a reading no participant could make (#843 option B) --------------------------------------

BOUND = 25.0 * bp._PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE


def drop(hist, scale=1.0, bound=BOUND):
    return bp._drop_implausible_power_samples(
        hist,
        power_scale=scale,
        max_plausible_kw=bound,
        participant_name="ev",
        power_sensor="sensor.ev_power",
    )


def at(hour):
    return DAY + timedelta(hours=hour)


def test_a_watt_reading_taken_as_kilowatts_is_dropped_and_real_ones_kept():
    real = [(at(8.874), -0.774994), (at(8.875), -0.42282)]
    assert drop([(at(8.851), 1514.417), (at(8.858), -774.818), *real]) == real
    hard = [(at(9), 24.9), (at(10), -25.0), (at(11), 0.0), (at(12), 30.0)]
    assert drop(hard) == hard  # at the cap, and a 1.2x overshoot, are real


def test_the_bound_is_in_kilowatts_after_the_live_scale():
    assert drop([(at(9), 24900.0)], scale=0.001) == [(at(9), 24900.0)]
    assert drop([(at(9), 1514417.0)], scale=0.001) == []
    assert drop([(at(9), -1514.417)]) == []  # magnitude, not sign


def test_a_too_low_configured_limit_still_keeps_the_real_readings():
    low = 5.0 * bp._PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE
    assert drop([(at(9), 24.9), (at(10), -25.0)], bound=low) == [
        (at(9), 24.9),
        (at(10), -25.0),
    ]
    assert drop([(at(9), 1514.417)], bound=low) == []


@pytest.mark.parametrize("bound", [0.0, -1.0])
def test_no_configured_limit_judges_nothing(bound):
    hist = [(at(9), 1514.417), (at(10), -0.77)]
    assert drop(hist, bound=bound) == hist
    assert drop([], bound=bound) == []


def test_a_dropped_reading_leaves_nothing_behind_in_the_hour():
    good = [(at(9) + timedelta(minutes=m), 6.0) for m in (10, 20, 30, 40, 50)]
    kept = drop([(at(9) + timedelta(minutes=5), 1514.417), *good])
    assert kept == good  # discarded, never clamped to the bound
    assert sw.resample_history_mean(kept, [at(9)], 1.0) == [pytest.approx(6.0)]
    alone = drop([(at(9) + timedelta(minutes=5), 1514.417)])
    assert sw.resample_history_mean(alone, [at(9)], 1.0) == [0.0]


# --- the report scores the fleet (#768) -------------------------------------------------------


def test_the_quality_report_scores_home_and_every_participant(site, monkeypatch):
    cfg = {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
    }
    series = {
        "sensor.real_load": 2.0,
        "sensor.import_price": 0.20,
        "sensor.export_price": 0.05,
        "sensor.ev_soc": 55.0,
    }

    def fetch(entity, _start, _end):
        return every(series.get(entity, 0.0))

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)
    monkeypatch.setattr(sw, "fetch_entity_power_history_kw", fetch)
    monkeypatch.setattr(sw.urllib.request, "urlopen", _refuse)
    now = END + timedelta(hours=10)
    assert sw.compute_daily_quality_report(cfg, now)["scored_participants"] == ["home"]
    site.add()
    report = sw.compute_daily_quality_report(cfg, now)
    assert report["scored_participants"] == ["home", "ev"]
    assert report["j_ach"] == pytest.approx(report["j_ref"], abs=1e-6)  # all idle


def _refuse(*_a, **_k):
    raise sw.urllib.error.URLError("no network in tests")


def test_a_participant_whose_positive_is_discharge_reads_it_so(site):
    site.add({**EV, "battery_participant_power_positive_is_charge": False})
    ((_b, charge, discharge, _f, _s),) = resolve()
    assert (charge == 0.0).all() and (discharge == 5.0).all()
