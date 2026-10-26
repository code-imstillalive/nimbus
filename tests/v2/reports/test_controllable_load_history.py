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
