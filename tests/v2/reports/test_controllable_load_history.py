"""What each controllable load really delivered on an elapsed day, from its
own power sensor's recorded history (#768) (#1837).

Real delivered energy, never the configured target; a period the recorder
has no fresh sample for credits nothing, where carrying the last reading
forward would invent energy; and a load that cannot be reconstructed says
why instead of reporting zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import load_run_state
import numpy as np
import pytest
import solver_shared as shared
import solver_writer as sw
from solver_inputs import controllable_load_history as clh

DAY_START = datetime(2026, 9, 20, 0, 0, tzinfo=sw.LOCAL_TZ)
DAY_END = DAY_START + timedelta(days=1)
GRID = [DAY_START + timedelta(hours=i) for i in range(24)]

HWS = {
    "controllable_load_name": "Hot water",
    "controllable_load_kind": "deferrable",
    "controllable_load_power_sensor": "sensor.hws_power",
    "deferrable_max_power_kw": 0.65,
    "deferrable_target_kwh": 4.0,  # deliberately not what it delivers
}
POOL = {
    "controllable_load_name": "Pool pump",
    "controllable_load_kind": "sheddable",
    "controllable_load_power_sensor": "sensor.pool_power",
    "sheddable_nominal_kw": 1.2,
}


@pytest.fixture
def site(monkeypatch):
    s = SimpleNamespace(history={}, subentries={}, discovered=None)
    entry = SimpleNamespace(entry_id="hub", subentries=s.subentries)
    monkeypatch.setattr(
        shared.NATIVE,
        "hass",
        SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            states=SimpleNamespace(get=lambda eid: None),
        ),
    )
    monkeypatch.setattr(
        sw,
        "fetch_entity_power_history_kw",
        lambda eid, start, end: s.history.get(eid, []),
    )
    monkeypatch.setattr(
        sw,
        "resolve_controllable_load_power_sensor",
        lambda data: data.get("controllable_load_power_sensor") or s.discovered,
    )

    def add(sid, data, kind="controllable_load"):
        s.subentries[sid] = SimpleNamespace(
            subentry_id=sid, subentry_type=kind, data=data
        )

    s.add = add
    return s


def _hourly(values, step_minutes=10):
    """A sample every 10 minutes through each hour: no period is stale."""
    out = []
    for hour, kw in enumerate(values):
        t = DAY_START + timedelta(hours=hour)
        while t < DAY_START + timedelta(hours=hour + 1):
            out.append((t, kw))
            t += timedelta(minutes=step_minutes)
    return out


def _resolve():
    return {
        d.subentry_id: d
        for d in clh.resolve_controllable_load_delivery_history(
            day_start=DAY_START,
            day_end=DAY_END,
            grid_times=GRID,
            period_hours=1.0,
            n_periods=len(GRID),
        )
    }


@pytest.mark.parametrize(
    ("kw", "hours", "stale", "by_period", "total"),
    [
        ([2.0] * 4, 1.0, set(), (2.0, 2.0, 2.0, 2.0), 8.0),
        ([2.0] * 4, 0.25, set(), (0.5, 0.5, 0.5, 0.5), 2.0),
        ([2.0] * 4, 1.0, {1, 2}, (2.0, 0.0, 0.0, 2.0), 4.0),
        ([3.0, -3.0, 3.0], 1.0, set(), (3.0, 0.0, 3.0), 6.0),  # a load never generates
    ],
)
def test_energy_is_power_times_time_with_stale_periods_and_negatives_at_zero(
    kw, hours, stale, by_period, total
):
    got, got_total = clh.integrate_delivered_kwh(kw, hours, stale)
    assert got == pytest.approx(by_period) and got_total == pytest.approx(total)


def test_each_load_reports_its_own_real_delivery_never_its_target(site):
    hws, pool = [0.0] * 24, [0.0] * 24
    hws[2] = hws[3] = hws[4] = 0.65
    for h in (10, 11, 12, 13):
        pool[h] = 1.2
    site.add("hws", HWS)
    site.add("pool", POOL)
    site.add("ev", {"battery_participant_name": "ev"}, kind="battery_participant")
    site.history = {
        "sensor.hws_power": _hourly(hws),
        "sensor.pool_power": _hourly(pool),
    }
    out = _resolve()
    assert set(out) == {"hws", "pool"}  # a battery participant is not a load
    assert out["hws"].delivered_kwh == pytest.approx(1.95)
    assert out["pool"].delivered_kwh == pytest.approx(4.8)
    assert [i for i, v in enumerate(out["hws"].delivered_kwh_by_period) if v] == [
        2,
        3,
        4,
    ]
    assert (out["hws"].kind, out["pool"].kind) == ("deferrable", "sheddable")
    assert (out["hws"].reason, out["hws"].observed_period_fraction) == (
        clh.REASON_OK,
        1.0,
    )
    assert out["hws"].sample_count == len(site.history["sensor.hws_power"])


def test_a_recorder_gap_credits_only_the_guards_own_hour(site, monkeypatch):
    """One reading at midnight: carried forward it would claim 28.8 kWh.
    The guard is the repository's own sample-gap constant, and widening it
    at its source moves the answer."""
    assert load_run_state.MAX_SAMPLE_GAP_HOURS == 1.0
    site.add("pool", POOL)
    site.history = {"sensor.pool_power": [(DAY_START, 1.2)]}
    out = _resolve()["pool"]
    assert out.delivered_kwh == pytest.approx(2.4) and out.stale_period_count == 22
    assert out.observed_period_fraction == pytest.approx(round(2 / 24, 4))
    real = sw._stale_power_period_indices
    monkeypatch.setattr(
        sw,
        "_stale_power_period_indices",
        lambda points, grid, hours, **kw: real(points, grid, hours, max_gap_hours=4.0),
    )
    wider = _resolve()["pool"]
    assert wider.delivered_kwh == pytest.approx(6.0) and wider.stale_period_count == 19


@pytest.mark.parametrize(
    ("configured", "rows", "reason"),
    [(False, None, clh.REASON_NO_POWER_SENSOR), (True, [], clh.REASON_NO_HISTORY)],
    ids=["no_sensor", "before_the_sensor_existed"],
)
def test_a_load_that_cannot_be_reconstructed_says_why_not_zero(
    site, configured, rows, reason
):
    data = dict(HWS)
    if not configured:
        del data["controllable_load_power_sensor"]
    site.add("hws", data)
    site.history = {"sensor.hws_power": rows or []}
    out = _resolve()["hws"]
    assert (out.scorable, out.reason, out.delivered_kwh) == (False, reason, None)
    assert out.observed_period_fraction is None and out.delivered_kwh_by_period == ()
    assert out.name == "Hot water"
    attr = out.as_attribute()
    assert attr["delivered_kwh"] is None and attr["reason"] == reason
    assert "delivered_kwh_by_period" not in attr  # never the per-period series


def test_a_discovered_sensor_is_used_when_none_is_configured(site):
    data = dict(HWS)
    del data["controllable_load_power_sensor"]
    site.add("hws", data)
    site.discovered = "sensor.hws_power"
    hws = [0.0] * 24
    hws[5] = 0.65
    site.history = {"sensor.hws_power": _hourly(hws)}
    out = _resolve()["hws"]
    assert (
        out.power_sensor == "sensor.hws_power"
        and out.delivered_kwh == pytest.approx(0.65)
    )


def test_outside_home_assistant_there_is_nothing_to_reconstruct(monkeypatch):
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    assert _resolve() == {}


# --- the oracle's controllable loads, built from what each really delivered (#1357) ------------


ORACLE_START = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
ORACLE_GRID = [ORACLE_START + timedelta(hours=i) for i in range(24)]
DISHWASHER = {
    "controllable_load_name": "Dishwasher",
    "controllable_load_kind": "deferrable",
    "deferrable_max_power_kw": 2.0,
    "deferrable_target_kwh": 99.0,  # never what the oracle is charged with
    "deferrable_earliest_hour": 0.0,
    "deferrable_deadline_hour": 23.0,
    "deferrable_shortfall_price": 4.0,
}
SPA = {
    "controllable_load_name": "Pool",
    "controllable_load_kind": "sheddable",
    "sheddable_shed_cost": 0.45,
    "sheddable_min_fraction": 0.25,
}


def _ran(
    sid="s1", name="Dishwasher", kind="deferrable", kwh=4.0, at=(18, 19), each=2.0, **kw
):
    return clh.ControllableLoadDelivery(
        subentry_id=sid,
        name=name,
        kind=kind,
        power_sensor="sensor.x",
        scorable=kw.pop("scorable", True),
        reason=kw.pop("reason", clh.REASON_OK),
        delivered_kwh=kwh,
        delivered_kwh_by_period=tuple(each if i in at else 0.0 for i in range(24)),
        n_periods=24,
    )


def _oracle(monkeypatch, deliveries, subs):
    """subs: {subentry_id: data}, or None for an install with no entry."""
    entries = []
    if subs is not None:
        entries = [
            SimpleNamespace(
                subentries={
                    sid: SimpleNamespace(
                        subentry_id=sid, subentry_type="controllable_load", data=data
                    )
                    for sid, data in subs.items()
                }
            )
        ]
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_entries=lambda _d: entries)
    )
    monkeypatch.setattr(sw.NATIVE, "hass", hass)
    return clh.build_oracle_controllable_loads(
        deliveries=deliveries,
        grid_times=ORACLE_GRID,
        period_hours=[1.0] * 24,
        n_periods=24,
        window_start=ORACLE_START,
    )


def test_with_nothing_delivered_or_no_entry_the_oracle_gets_nothing(monkeypatch):
    assert _oracle(monkeypatch, [], {"s1": DISHWASHER}) is None
    assert _oracle(monkeypatch, [_ran()], None) is None


def test_a_deferrable_load_is_charged_with_what_it_delivered_never_its_target(
    monkeypatch,
):
    (load,) = _oracle(monkeypatch, [_ran(kwh=4.0)], {"s1": DISHWASHER}).adequacy
    assert (
        load.target_kwh,
        load.max_power_kw,
        load.shortfall_price,
        load.subentry_id,
    ) == (4.0, 2.0, 4.0, "s1")
    other = _oracle(
        monkeypatch, [_ran()], {"s1": {**DISHWASHER, "deferrable_target_kwh": 1.0}}
    )
    assert other.adequacy[0].target_kwh == 4.0


def test_a_sheddable_load_may_shed_what_it_really_drew(monkeypatch):
    (load,) = _oracle(
        monkeypatch, [_ran(kind="sheddable", name="Pool")], {"s1": SPA}
    ).sheddable
    assert [float(load.forecast_kw[i]) for i in (0, 18, 19)] == [0.0, 2.0, 2.0]
    assert (load.shed_cost, load.min_fraction) == (0.45, 0.25)


@pytest.mark.parametrize(
    ("delivery", "data", "reason"),
    [
        (
            _ran(kind="thermal", name="Tank"),
            {"controllable_load_kind": "thermal"},
            clh.ORACLE_SKIP_THERMAL,
        ),
        (
            _ran(scorable=False, reason="no power sensor configured"),
            DISHWASHER,
            "no power sensor configured",
        ),
        (_ran(kwh=0.0, each=0.0), DISHWASHER, clh.ORACLE_SKIP_NO_ENERGY),
        (_ran(kind="something_new"), DISHWASHER, clh.ORACLE_SKIP_UNKNOWN_KIND),
        (
            _ran(),
            {
                **DISHWASHER,
                "deferrable_earliest_hour": 22.0,
                "deferrable_deadline_hour": 2.0,
            },
            clh.ORACLE_SKIP_NO_WINDOW,
        ),
    ],
    ids=["thermal", "unscorable", "no_energy", "unknown_kind", "inverted_window"],
)
def test_every_load_left_out_is_named_with_why(monkeypatch, delivery, data, reason):
    bundle = _oracle(monkeypatch, [delivery], {"s1": data})
    assert (bundle.adequacy, bundle.sheddable) == ((), ())
    assert bundle.skipped == ((delivery.name, reason),)
    assert (
        bundle.delivered_kwh_by_period is None
    )  # nothing taken that is not given back


def test_only_the_loads_given_to_the_oracle_are_taken_out_of_its_base_load(monkeypatch):
    tank = _ran(sid="s2", name="Tank", kind="thermal", at=(3,), each=5.0)
    bundle = _oracle(monkeypatch, [_ran(), tank], {"s1": DISHWASHER, "s2": {}})
    taken = np.asarray(bundle.delivered_kwh_by_period, float)
    assert float(taken.sum()) == pytest.approx(4.0) and float(taken[3]) == 0.0
    pool = _ran(sid="s2", name="Pool", kind="sheddable", kwh=3.0, at=(12, 13), each=1.5)
    both = _oracle(monkeypatch, [_ran(), pool], {"s1": DISHWASHER, "s2": SPA})
    assert float(
        np.asarray(both.delivered_kwh_by_period, float).sum()
    ) == pytest.approx(7.0)
    assert (both.adequacy[0].name, both.sheddable[0].name) == ("Dishwasher", "Pool")


def test_scoring_controllable_loads_is_off_unless_switched_on():
    from const import DEFAULT_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED

    assert DEFAULT_SOLVER_SCORE_CONTROLLABLE_LOADS_ENABLED is False


def test_an_elapsed_days_window_opens_at_its_own_hour_not_at_the_start(monkeypatch):
    """The oracle is scoring a day already over: a 06:00-16:00 window opens at
    its own hour, never at period 0 the way a window already in progress
    does on a live solve (#1357)."""
    window = {
        **DISHWASHER,
        "deferrable_earliest_hour": 6.0,
        "deferrable_deadline_hour": 16.0,
    }
    (load,) = _oracle(monkeypatch, [_ran(at=(8, 9))], {"s1": window}).adequacy
    assert load.earliest_period > 0  # an in-progress window would open at 0
