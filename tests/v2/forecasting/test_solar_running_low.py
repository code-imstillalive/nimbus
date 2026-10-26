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

from datetime import datetime, timedelta

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


def test_it_does_not_judge_while_either_forecast_is_under_the_minimum(install):
    for minimum, reason, active in [
        (None, "below_minimum", False),
        (1.5, "judged", True),
    ]:

        def at(t):
            return {6.5: 1.9, 7.0: 3.0}.get(t.hour + t.minute / 60, 0.0)

        now = TODAY.replace(hour=7, minute=5)
        site = Site(_day(TODAY, at, lambda t: 0.5), history=_held(now, 0))
        cfg = (
            _cfg()
            if minimum is None
            else _cfg(solver_solar_running_low_min_p50_kw=minimum)
        )
        r = _judge(install, site, now, cfg)
        assert (r["reason"], r["active"]) == (reason, active)
        if minimum is None:
            assert site.history_calls == 0  # not even read


# --- on, off, and held -------------------------------------------------------------------------


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


# --- inert, off, and a recorder that is behind -------------------------------------------------


def test_without_what_it_needs_it_is_off_even_if_it_was_on(install):
    # test_without_what_it_needs_it_is_off_even_if_it_was_on
    for history, cfg_over, reason in [
        ([], {}, "no_history"),
        (RuntimeError("recorder down"), {}, "no_history"),
        (None, {"solver_solar_running_low_min_p50_kw": 0}, "off"),
        (None, {"solver_solar_power_sensor": None}, "no_solar_power_sensor"),
    ]:
        today = _day(TODAY, *FLAT)
        now = TODAY.replace(hour=10)
        assert _judge(install, Site(today, history=_held(now, 500)), now)["active"]
        later = now + timedelta(minutes=5)
        site = Site(today, history=_held(later, 0) if history is None else history)
        r = _judge(install, site, later, _cfg(**cfg_over))
        assert (r["reason"], r["active"]) == (reason, False)
        if reason == "off":
            assert site.history_calls == 0

    # test_a_locked_recorder_is_not_judged_even_if_it_was_on: A backup locks the recorder: the history read returns the last
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

    # test_behind_means_more_than_five_minutes_and_an_unreadable_state_is_not_behind
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

    # test_each_member_is_lowered_by_its_own_path_and_the_ratio_is_floored
    for history_w, scale in [(2000, 0.5), (400, 0.3)]:
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

    # test_solcast_without_p10_is_scaled_never_zeroed: A missing pv_estimate10 reads as 0.0; taken as a lower bound it
    now = TODAY.replace(hour=10, minute=3)
    site = Site(_day(TODAY, _daylight(4.0)), history=_held(now, 2000))
    (kw, lo, _up), pub = _build(install, site, now, [now, TODAY.replace(hour=11)])
    assert pub["active"] and pub["members_ratio_scaled"] == 1
    assert kw[1] == pytest.approx(2.0) and lo[1] <= kw[1]

    # test_without_solcast_the_first_solve_waits_then_judges_from_memory
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


def _ramp():
    return _day(TODAY, _daylight(5.0), _daylight(2.0))


# --- what it does to the forecast --------------------------------------------------------------


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


# --- what is published --------------------------------------------------------------------------
