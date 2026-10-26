"""The solar "running low" gate (#1784) (#1837).

When measured PV over the last half hour runs well below the forecast, the
rest of TODAY is planned on a lower solar forecast. The reference is the
lower of the forecast now and 30 minutes ago (a 30-minute mean trails a
rising morning by its own length); it goes on below 70 % only after a
10-minute confirmation, stays on until above 90 %, never judges under a
2 kW forecast or from a recorder that is behind, and lowers each member of
the blend by its own path. All of it is a setting; with it off the plan is
byte for byte what it was.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import const
import pytest
import solver_shared as shared
import solver_writer as sw
from solver_inputs import solar
from solver_inputs import solar_running_low as srl

TODAY = datetime(2026, 10, 16, tzinfo=sw.LOCAL_TZ)
TOMORROW = TODAY + timedelta(days=1)
SENSOR = "sensor.pv_power"
TODAY_E = "sensor.solcast_pv_forecast_forecast_today"
TOMORROW_E = "sensor.solcast_pv_forecast_forecast_tomorrow"
KNOWN = {"solcast": [TODAY_E, TOMORROW_E]}


def _day(day, p50_at, p10_at=None):
    rows = []
    for k in range(48):
        start = day + timedelta(minutes=30 * k)
        row = {
            "period_start": start.isoformat(),
            "pv_estimate": p50_at(start),
            "pv_estimate90": p50_at(start) * 1.2,
        }
        if p10_at is not None:
            row["pv_estimate10"] = p10_at(start)
        rows.append(row)
    return rows


def _daylight(level):
    return lambda t: level if 6 <= t.hour < 18 else 0.0


FLAT = (_daylight(4.0), _daylight(1.5))


class Site:
    """Solcast today and tomorrow, a PV sensor in watts with a recorded
    history, and its live reading."""

    def __init__(
        self, today=None, tomorrow=None, history=None, live_w="500", live_changed=None
    ):
        self.states = {}
        if today is not None:
            self.states[TODAY_E] = {"detailedForecast": today}
        if tomorrow is not None:
            self.states[TOMORROW_E] = {"detailedForecast": tomorrow}
        self.history = history or []
        self.live_w, self.live_changed = live_w, live_changed
        self.history_calls = 0

    def ha_get(self, eid):
        if eid == SENSOR:
            out = {"state": self.live_w, "attributes": {"unit_of_measurement": "W"}}
            if self.live_changed is not None:
                out["last_changed"] = self.live_changed.isoformat()
            return out
        return {"attributes": self.states[eid]}

    def fetch(self, eid, start, end):
        self.history_calls += 1
        if isinstance(self.history, Exception):
            raise self.history
        return [(t, v) for t, v in self.history if t < end]


@pytest.fixture
def install(monkeypatch):
    srl.reset_gate_state()
    monkeypatch.setattr(sw, "_SOLAR_SOURCE_WARNED", set())

    def use(site):
        monkeypatch.setattr(sw, "ha_get", site.ha_get)
        monkeypatch.setattr(shared, "ha_get", site.ha_get)
        monkeypatch.setattr(
            sw, "entity_exists", lambda e: e in site.states or e == SENSOR
        )
        monkeypatch.setattr(sw, "fetch_entity_history_range", site.fetch)
        monkeypatch.setattr(sw, "_kw_scale_factor", lambda e: 0.001)
        return site

    yield use
    srl.reset_gate_state()


def _held(now, watts):
    return [(now - timedelta(minutes=30), float(watts))]


def _cfg(**over):
    cfg = {
        "solver_auto_include_known_solar": True,
        "solver_solar_power_sensor": SENSOR,
        "solver_solar_running_low_confirm_minutes": 0,
        "known_sources": KNOWN,
    }
    cfg.update(over)
    return cfg


def _judge(install, site, now, cfg=None):
    install(site)
    return srl.evaluate_gate(sw, cfg or _cfg(), now, None)[0]


def _build(install, site, now, grid, cfg=None):
    install(site)
    arrays = solar.build_solar_arrays(cfg or _cfg(), grid, len(grid), now)
    return arrays, srl.published_result()


# --- what it compares against ------------------------------------------------------------------


def test_a_rising_morning_is_judged_against_the_lagged_forecast_and_does_not_trip(
    install,
):
    p50 = {6.5: 2.2, 7.0: 4.0, 7.5: 5.5}

    def at(t):
        return p50.get(t.hour + t.minute / 60, 0.0 if t.hour < 6 else 6.0)

    now = TODAY.replace(hour=7, minute=10)
    r = _judge(
        install,
        Site(_day(TODAY, at, lambda t: at(t) * 0.5), history=_held(now, 2000)),
        now,
    )
    assert (r["reason"], r["p50_now_kw"], r["p50_lagged_kw"], r["reference_kw"]) == (
        "judged",
        4.0,
        2.2,
        2.2,
    )
    assert not r["active"]


def test_on_a_falling_afternoon_the_lower_forecast_is_the_reference(install):
    def at(t):
        return {14.0: 5.0, 14.5: 3.0}.get(t.hour + t.minute / 60, 4.0)

    now = TODAY.replace(hour=14, minute=40)
    r = _judge(
        install, Site(_day(TODAY, at, lambda t: 1.0), history=_held(now, 2400)), now
    )
    assert r["reference_kw"] == 3.0 and not r["active"]


@pytest.mark.parametrize(
    ("minimum", "reason", "active"),
    [(None, "below_minimum", False), (1.5, "judged", True)],
)
def test_it_does_not_judge_while_either_forecast_is_under_the_minimum(
    install, minimum, reason, active
):
    def at(t):
        return {6.5: 1.9, 7.0: 3.0}.get(t.hour + t.minute / 60, 0.0)

    now = TODAY.replace(hour=7, minute=5)
    site = Site(_day(TODAY, at, lambda t: 0.5), history=_held(now, 0))
    cfg = (
        _cfg() if minimum is None else _cfg(solver_solar_running_low_min_p50_kw=minimum)
    )
    r = _judge(install, site, now, cfg)
    assert (r["reason"], r["active"]) == (reason, active)
    if minimum is None:
        assert site.history_calls == 0  # not even read


# --- on, off, and held -------------------------------------------------------------------------


def test_on_below_70_percent_and_held_until_above_90(install):
    today = _day(TODAY, *FLAT)
    steps = [
        (0, 2900, False),
        (5, 2700, True),
        (10, 3400, True),
        (15, 3500, True),
        (20, 3700, False),
        (25, 3000, False),
        (30, 2600, True),
    ]
    for minute, watts, expected in steps:
        now = TODAY.replace(hour=10, minute=minute)
        assert (
            _judge(install, Site(today, history=_held(now, watts)), now)["active"]
            is expected
        ), minute


def test_the_thresholds_are_settings(install):
    now = TODAY.replace(hour=10)
    cfg = _cfg(
        solver_solar_running_low_on_percent=50, solver_solar_running_low_off_percent=60
    )
    assert not _judge(
        install, Site(_day(TODAY, *FLAT), history=_held(now, 2200)), now, cfg
    )["active"]
    assert _judge(
        install, Site(_day(TODAY, *FLAT), history=_held(now, 1900)), now, cfg
    )["active"]


def test_on_since_is_when_it_went_on_and_clears_when_off(install):
    today = _day(TODAY, *FLAT)
    on_at = TODAY.replace(hour=10)
    _judge(install, Site(today, history=_held(on_at, 1000)), on_at)
    later = on_at + timedelta(minutes=5)
    assert (
        _judge(install, Site(today, history=_held(later, 1000)), later)["on_since"]
        == on_at.isoformat()
    )
    done = later + timedelta(minutes=5)
    assert (
        _judge(install, Site(today, history=_held(done, 4000)), done)["on_since"]
        is None
    )


def _confirmed(install, readings, extra=None):
    """Active after each (minute after 07:00, ratio of a flat 4 kW)."""
    today = _day(TODAY, *FLAT)
    cfg = {"solver_solar_power_sensor": SENSOR, "known_sources": KNOWN, **(extra or {})}
    out = []
    for minute, ratio in readings:
        now = TODAY.replace(hour=7) + timedelta(minutes=minute)
        out.append(
            _judge(install, Site(today, history=_held(now, ratio * 4000)), now, cfg)[
                "active"
            ]
        )
    return out


@pytest.mark.parametrize(
    ("readings", "expected"),
    [
        ([(0, 0.55), (5, 0.57), (9, 0.56), (10, 0.58)], [False, False, False, True]),
        (
            [(0, 0.55), (5, 0.75), (10, 0.58), (15, 0.6)],
            [False] * 4,
        ),  # recovered in the window
        (
            [(0, 0.5), (10, 0.5), (11, 0.95)],
            [False, True, False],
        ),  # release is immediate
    ],
)
def test_it_goes_on_only_after_ten_minutes_of_low_readings(install, readings, expected):
    assert _confirmed(install, readings) == expected


def test_16_october_late_start_does_not_trip_it_but_would_without_confirmation(install):
    """The reference household's minute-by-minute ratios, 07:30-07:47:
    crossing 0.70 at 07:38, so a 10-minute window never trips."""
    ratios = [0.610, 0.621, 0.632, 0.644, 0.656, 0.669, 0.680, 0.694, 0.711, 0.729]
    ratios += [0.749, 0.771, 0.794, 0.814, 0.837, 0.859, 0.881, 0.903]
    readings = [(30 + i, r) for i, r in enumerate(ratios)]
    assert _confirmed(install, readings) == [False] * len(readings)
    srl.reset_gate_state()
    assert _confirmed(
        install, readings, {"solver_solar_running_low_confirm_minutes": 0}
    )[0]


def test_pending_since_is_published_while_confirming(install):
    today = _day(TODAY, *FLAT)
    cfg = {"solver_solar_power_sensor": SENSOR, "known_sources": KNOWN}
    t0 = TODAY.replace(hour=10)
    r = _judge(install, Site(today, history=_held(t0, 2000)), t0, cfg)
    assert (r["pending_since"], r["active"]) == (t0.isoformat(), False)
    t1 = t0 + timedelta(minutes=10)
    r = _judge(install, Site(today, history=_held(t1, 2000)), t1, cfg)
    assert r["active"] and r["pending_since"] is None


def test_one_warning_when_it_goes_on_and_one_info_when_it_goes_off(install, caplog):
    today = _day(TODAY, *FLAT)
    with caplog.at_level(logging.DEBUG, logger=shared._LOGGER.name):
        for minute, watts in ((0, 500), (5, 500), (10, 500), (15, 3900), (20, 3900)):
            now = TODAY.replace(hour=10, minute=minute)
            _judge(install, Site(today, history=_held(now, watts)), now)
    changes = [r for r in caplog.records if "#1784" in r.getMessage()]
    assert [r.levelname for r in changes] == ["WARNING", "INFO"]
    assert "OFF" in changes[1].getMessage()


def test_after_a_restart_it_rejudges_from_the_recorder_straight_away(install):
    today = _day(TODAY, *FLAT)
    now = TODAY.replace(hour=12)
    _judge(install, Site(today, history=_held(now, 500)), now)
    srl.reset_gate_state()
    assert not srl._STATE.active
    later = now + timedelta(minutes=1)
    r = _judge(install, Site(today, history=_held(later, 500)), later)
    assert r["active"] and r["on_since"] == later.isoformat()


# --- inert, off, and a recorder that is behind -------------------------------------------------


@pytest.mark.parametrize(
    ("history", "cfg_over", "reason"),
    [
        ([], {}, "no_history"),
        (RuntimeError("recorder down"), {}, "no_history"),
        (None, {"solver_solar_running_low_min_p50_kw": 0}, "off"),
        (None, {"solver_solar_power_sensor": None}, "no_solar_power_sensor"),
    ],
    ids=["no_history", "history_raises", "minimum_zero", "no_pv_sensor"],
)
def test_without_what_it_needs_it_is_off_even_if_it_was_on(
    install, history, cfg_over, reason
):
    today = _day(TODAY, *FLAT)
    now = TODAY.replace(hour=10)
    assert _judge(install, Site(today, history=_held(now, 500)), now)["active"]
    later = now + timedelta(minutes=5)
    site = Site(today, history=_held(later, 0) if history is None else history)
    r = _judge(install, site, later, _cfg(**cfg_over))
    assert (r["reason"], r["active"]) == (reason, False)
    if reason == "off":
        assert site.history_calls == 0


def test_without_any_forecast_there_is_no_reference(install):
    now = TODAY.replace(hour=10)
    install(Site())
    solar.build_solar_arrays(
        {"solver_solar_power_sensor": SENSOR, "known_sources": KNOWN}, [now], 1, now
    )
    assert (srl.published_result()["reason"], srl.published_result()["active"]) == (
        "no_reference",
        False,
    )


def _ramp():
    return _day(TODAY, _daylight(5.0), _daylight(2.0))


def test_a_locked_recorder_is_not_judged_even_if_it_was_on(install):
    """A backup locks the recorder: the history read returns the last
    state before the lock, held to now, while the panels ramp up."""
    now = TODAY.replace(hour=8, minute=30)
    early = now - timedelta(hours=1)
    assert _judge(install, Site(_ramp(), history=_held(early, 1000)), early)["active"]
    behind = Site(
        _ramp(),
        history=_held(now, 1000),
        live_w="8000",
        live_changed=now - timedelta(seconds=10),
    )
    r = _judge(install, behind, now)
    assert (r["reason"], r["active"], r["actual_30min_avg_kw"]) == (
        "recorder_behind",
        False,
        None,
    )
    srl.reset_gate_state()
    assert _judge(install, Site(_ramp(), history=_held(now, 1000)), now)[
        "active"
    ]  # without the live check


def test_behind_means_more_than_five_minutes_and_an_unreadable_state_is_not_behind():
    assert srl.RECORDER_BEHIND_AFTER == timedelta(minutes=5)
    hist = [(TODAY.replace(hour=10), 1.0)]
    on_edge = Site(_ramp(), live_changed=TODAY.replace(hour=10, minute=5))
    past = Site(_ramp(), live_changed=TODAY.replace(hour=10, minute=5, second=1))
    assert srl.recorder_behind_by(on_edge, SENSOR, hist) is None
    assert srl.recorder_behind_by(past, SENSOR, hist) == timedelta(minutes=5, seconds=1)
    assert srl.recorder_behind_by(Site(_ramp()), SENSOR, hist) is None

    class Gone:
        def ha_get(self, _e):
            raise RuntimeError("gone")

    assert srl.recorder_behind_by(Gone(), SENSOR, hist) is None


# --- what it does to the forecast --------------------------------------------------------------


def test_only_todays_remaining_periods_move_to_p10(install):
    now = TODAY.replace(hour=10, minute=3)
    grid = [now] + [
        TODAY.replace(hour=10, minute=30) + timedelta(hours=k) for k in range(30)
    ]
    site = Site(
        _day(TODAY, *FLAT),
        _day(TOMORROW, *FLAT),
        history=_held(now, 1000),
        live_w="1100",
    )
    (kw, lo, up), pub = _build(install, site, now, grid)
    assert pub["active"] and kw[0] == pytest.approx(1.1)
    for i, t in enumerate(grid[1:], start=1):
        expected = (
            (1.5 if t.date() == TODAY.date() else 4.0) if 6 <= t.hour < 18 else 0.0
        )
        assert kw[i] == pytest.approx(expected) and lo[i] <= kw[i] <= up[i]
    assert pub["periods_lowered"] == sum(
        1 for t in grid[1:] if t.date() == TODAY.date()
    )


def test_while_off_the_plan_is_byte_for_byte_what_it_was(install):
    now = TODAY.replace(hour=10, minute=3)
    grid = [now + timedelta(minutes=30 * k) for k in range(60)]
    healthy = Site(_day(TODAY, *FLAT), _day(TOMORROW, *FLAT), history=_held(now, 3900))
    judged, pub = _build(install, healthy, now, grid)
    assert not pub["active"]
    srl.reset_gate_state()
    off, pub = _build(
        install, healthy, now, grid, _cfg(solver_solar_running_low_min_p50_kw=0)
    )
    assert pub["reason"] == "off" and judged == off


def _blend_site(history_w):
    """Solcast (p50 4, p10 1.5), Open-Meteo at 6 kW with no band, and a
    generic forecast at 5 kW with a lower bound of 3.5."""
    now = TODAY.replace(hour=10, minute=3)
    site = Site(
        _day(TODAY, *FLAT), _day(TOMORROW, *FLAT), history=_held(now, history_w)
    )
    hours = [TODAY + timedelta(hours=h) for h in range(48)]
    site.states["sensor.om_today"] = {"watts": {t.isoformat(): 6000.0 for t in hours}}
    site.states["sensor.own"] = {
        "forecast": [
            {"time": t.isoformat(), "value": 5.0, "lower": 3.5, "upper": 6.5}
            for t in hours
        ]
    }
    cfg = _cfg(
        solver_solar_forecast_sensor="sensor.own",
        known_sources={**KNOWN, "open_meteo": ["sensor.om_today"]},
    )
    return site, now, [now, TODAY.replace(hour=11), TOMORROW.replace(hour=11)], cfg


@pytest.mark.parametrize(
    ("history_w", "scale"), [(2000, 0.5), (400, 0.3)], ids=["half", "floored"]
)
def test_each_member_is_lowered_by_its_own_path_and_the_ratio_is_floored(
    install, history_w, scale
):
    site, now, grid, cfg = _blend_site(history_w)
    (kw, lo, _up), pub = _build(install, site, now, grid, cfg)
    assert pub["active"] and pub["reference_source"] == "solcast"
    assert (
        pub["members_p10"],
        pub["members_lower_bound"],
        pub["members_ratio_scaled"],
    ) == (1, 1, 1)
    assert pub["ratio_scale_used"] == scale
    assert kw[1] == pytest.approx((3.5 + 6.0 * scale + 1.5) / 3) and lo[1] <= kw[1]
    assert kw[2] == pytest.approx((5.0 + 6.0 + 4.0) / 3)  # tomorrow untouched


def test_solcast_without_p10_is_scaled_never_zeroed(install):
    """A missing pv_estimate10 reads as 0.0; taken as a lower bound it
    would zero the rest of today."""
    now = TODAY.replace(hour=10, minute=3)
    site = Site(_day(TODAY, _daylight(4.0)), history=_held(now, 2000))
    (kw, lo, _up), pub = _build(install, site, now, [now, TODAY.replace(hour=11)])
    assert pub["active"] and pub["members_ratio_scaled"] == 1
    assert kw[1] == pytest.approx(2.0) and lo[1] <= kw[1]


# --- without Solcast, the blend is its own reference -------------------------------------------


def _own(value, history_w, now):
    site = Site(history=_held(now, history_w))
    hours = [TODAY + timedelta(hours=h) for h in range(48)]
    site.states = {
        "sensor.own": {
            "forecast": [
                {"time": t.isoformat(), "value": value(t), "lower": 0.6 * value(t)}
                for t in hours
            ]
        }
    }
    return site


OWN = {
    "solver_solar_forecast_sensor": "sensor.own",
    "solver_solar_power_sensor": SENSOR,
    "solver_solar_running_low_confirm_minutes": 0,
}


def test_without_solcast_the_first_solve_waits_then_judges_from_memory(install):
    first = TODAY.replace(hour=10)
    _arrays, pub = _build(
        install,
        _own(lambda t: 4.0, 1000, first),
        first,
        [first, first + timedelta(hours=1)],
        OWN,
    )
    assert (pub["reason"], pub["reference_source"], pub["active"]) == (
        "no_reference",
        "blend",
        False,
    )
    later = first + timedelta(minutes=30)
    (kw, _lo, _up), pub = _build(
        install,
        _own(lambda t: 4.0, 1000, later),
        later,
        [later, later + timedelta(hours=1)],
        OWN,
    )
    assert (
        pub["reason"] == "judged" and pub["active"] and pub["members_lower_bound"] == 1
    )
    assert kw[1] == pytest.approx(2.4)
    stale = later + timedelta(
        minutes=45
    )  # a remembered blend older than 40 min is not the lag
    assert (
        _build(install, _own(lambda t: 4.0, 1000, stale), stale, [stale], OWN)[1][
            "reason"
        ]
        == "no_reference"
    )


def test_the_lag_rule_holds_on_the_blend_too(install):
    first = TODAY.replace(hour=7)
    switch = TODAY.replace(hour=7, minute=30)

    def rising(t):
        return 2.2 if t < switch else 4.0

    _build(install, _own(rising, 0, first), first, [first], OWN)
    later = first + timedelta(minutes=30)
    pub = _build(install, _own(rising, 2000, later), later, [later], OWN)[1]
    assert (pub["reason"], pub["reference_kw"], pub["active"]) == ("judged", 2.2, False)


# --- what is published --------------------------------------------------------------------------


def test_every_reason_publishes_the_same_keys_and_the_settings_match_const():
    keys = set(srl._gate_result("not_evaluated"))
    for reason in (
        "off",
        "no_reference",
        "no_history",
        "recorder_behind",
        "judged",
        "error",
    ):
        assert set(srl._gate_result(reason)) == keys
    assert {
        "active",
        "reason",
        "ratio",
        "on_since",
        "pending_since",
        "reference_source",
    } <= keys
    assert const.CONF_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW == srl.CFG_MIN_P50_KW
    assert const.CONF_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT == srl.CFG_ON_PERCENT
    assert const.CONF_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT == srl.CFG_OFF_PERCENT
    assert const.DEFAULT_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW == srl.DEFAULT_MIN_P50_KW
    assert const.DEFAULT_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT == srl.DEFAULT_ON_PERCENT
    assert const.DEFAULT_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT == srl.DEFAULT_OFF_PERCENT
