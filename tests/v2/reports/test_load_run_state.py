"""A controllable load's run state: energy delivered from its power
readings, its daily quota and carry, the debounced command, the activation
and reaffirm limits, and the schedule view its sensors show (#1837).

Pure: no Home Assistant. Covers what lived in #476, #768, #828, #873, #1067,
#1360 and the load-run-state files.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import load_run_state as lrs
import pytest

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
DAY = "2026-09-20"


# --- energy from power readings ------------------------------------------------------------


def _sample(state, minutes, kw, **kw_args):
    return lrs.apply_power_sample(
        state,
        now=T0 + timedelta(minutes=minutes),
        day_key=kw_args.pop("day_key", DAY),
        power_kw=kw,
        **kw_args,
    )


def test_delivered_energy_and_cost_integrate_each_reading_over_the_gap_before_it():
    # test_delivered_energy_and_cost_integrate_each_reading_over_the_gap_before_it
    # test_delivered_energy_and_cost_integrate_each_reading_over_the_gap_before_it
    s = _sample(lrs.LoadRunState(), 0, 2.0)
    assert (s.delivered_today_kwh, s.currently_on, s.on_since) == (
        0.0,
        True,
        T0.timestamp(),
    )
    s = _sample(s, 30, 2.0, import_price_now=0.3)
    assert s.delivered_today_kwh == pytest.approx(
        1.0
    ) and s.cost_today == pytest.approx(0.3)
    s = _sample(s, 45, 0.01)
    assert (
        s.currently_on is False
        and s.off_since == (T0 + timedelta(minutes=45)).timestamp()
    )
    assert s.delivered_today_kwh == pytest.approx(1.0025)

    # test_a_new_day_rolls_the_unmet_quota_over_up_to_its_cap
    s = replace(
        lrs.LoadRunState(),
        day_key="2026-09-19",
        delivered_today_kwh=2.0,
        carry_kwh=1.0,
        cost_today=5.0,
    )
    s = _sample(s, 0, 0.0, quota_kwh_per_day=4.0, carry_cap_kwh=2.5)
    assert (s.day_key, s.delivered_today_kwh, s.cost_today) == (DAY, 0.0, 0.0)
    assert s.carry_kwh == 2.5  # 4 + 1 - 2 = 3, capped at 2.5
    first = _sample(
        lrs.LoadRunState(), 0, 0.0, quota_kwh_per_day=4.0, carry_cap_kwh=9.0
    )
    assert first.carry_kwh == 0.0  # no yesterday to carry from

    # test_todays_target_is_quota_plus_carry_capped_and_the_rest_is_never_negative
    assert (
        lrs.effective_target_kwh(
            quota_kwh_per_day=4.0, carry_kwh=2.0, max_per_day_kwh=5.0
        )
        == 5.0
    )
    assert (
        lrs.effective_target_kwh(
            quota_kwh_per_day=4.0, carry_kwh=2.0, max_per_day_kwh=None
        )
        == 6.0
    )
    assert lrs.remaining_kwh(target_kwh=5.0, delivered_today_kwh=6.0) == 0.0

    # --- activation and reaffirm limits -----------------------------------------------------------------
    # test_activations_are_counted_per_day_against_the_limit
    s = lrs.LoadRunState()
    assert lrs.activation_allowed(s, max_activations_per_day=None, day_key=DAY)
    s = lrs.record_activation(lrs.record_activation(s, day_key=DAY), day_key=DAY)
    assert s.activations_today == 2
    assert not lrs.activation_allowed(s, max_activations_per_day=2, day_key=DAY)
    assert lrs.activation_allowed(s, max_activations_per_day=2, day_key="2026-09-21")
    assert lrs.record_activation(s, day_key="2026-09-21").activations_today == 1

    # test_a_load_that_ignored_its_command_is_reaffirmed_within_limits
    for state, kw, allowed in [
        (_diverged(1200.0), {}, True),
        (_diverged(60.0), {}, False),  # not diverged long enough
        (
            _diverged(60.0, last_dispatch_failed=True),
            {},
            True,
        ),  # a failed send is retried
        (
            _diverged(1200.0, last_reaffirm_at=10_000.0 - 60),
            {},
            False,
        ),  # reaffirmed just now
        (
            _diverged(1200.0, reaffirms_today=20, reaffirms_today_day_key=DAY),
            {},
            False,
        ),  # daily cap
        (_diverged(1200.0), {"reaffirm_after_seconds": 0}, False),  # switched off
        (lrs.LoadRunState(), {}, False),  # nothing diverged
    ]:
        assert (
            lrs.reaffirm_allowed(state, now_ts=10_000.0, day_key=DAY, **kw) is allowed
        )

    # test_reaffirms_are_counted_per_day
    s = lrs.record_reaffirm(lrs.LoadRunState(), now_ts=5.0, day_key=DAY)
    s = lrs.record_reaffirm(s, now_ts=6.0, day_key=DAY)
    assert (s.reaffirms_today, s.last_reaffirm_at) == (2, 6.0)
    assert lrs.record_reaffirm(s, now_ts=7.0, day_key="2026-09-21").reaffirms_today == 1

    # --- why a load is or is not running -------------------------------------------------------------------
    # test_the_status_reason_says_running_deferred_or_not_scheduled
    times = [T0 + timedelta(minutes=30 * i) for i in range(4)]
    reason = lambda kw, price, lo=0, hi=3: lrs.compute_load_status_reason(
        power_kw=kw,
        shadow_price=price,
        grid_times=times,
        earliest_period=lo,
        deadline_period=hi,
    )
    assert (
        reason([2, 0, 0, 0], [0.3, 0.1, 0.1, 0.1])
        == "running now, marginal cost 30.00 c/kWh"
    )
    assert (
        reason([0, 0, 2, 0], [0.3, 0.2, 0.1, 0.1])
        == f"deferred to {times[2]:%H:%M}, saves 20.00 c/kWh"
    )
    assert (
        reason([0, 0, 2, 0], [0.1, 0.2, 0.3, 0.1])
        == f"deferred to {times[2]:%H:%M}, marginal cost 30.00 c/kWh"
    )
    assert reason([0, 0, 0, 2], [0.3] * 4, hi=2) == "not scheduled to run this window"
    assert reason([], []) is None

    # test_the_next_run_its_length_energy_and_cost
    s = replace(
        lrs.LoadRunState(),
        plan_forecast=_plan([0, 2, 2, 0, 3]),
        plan_cost_forecast=_plan([0, 0.2, 0.3, 0, 0.5]),
        plan_delivered_kwh_forecast=_plan([0, 1, 2, 2, 3.5]),
        plan_target_kwh=5.0,
    )
    view = lrs.derive_schedule_view(s, load_kind="deferrable", now=T0)
    assert (view.next_start, view.next_end) == (
        T0 + timedelta(minutes=30),
        T0 + timedelta(minutes=90),
    )
    assert (view.planned_duration_h, view.planned_energy_kwh, view.planned_cost) == (
        1.0,
        2.0,
        0.5,
    )
    assert (
        view.target_today_kwh == 5.0
        and view.status == f"scheduled {view.next_start:%H:%M}–{view.next_end:%H:%M}"
    )
    last = lrs.derive_schedule_view(
        s, load_kind="sheddable", now=T0 + timedelta(minutes=100)
    )
    assert (
        last.next_end is None and last.planned_energy_kwh == 1.5
    )  # from the power, not a delivered series
    assert last.target_today_kwh is None

    # test_the_status_line_says_the_one_thing_that_matters_most
    # test_the_status_line_says_the_one_thing_that_matters_most
    for state, kind, kw, status in [
        ({"commanded_state": True}, "deferrable", {}, "running"),
        ({"plan_shortfall_kwh": 1.5}, "deferrable", {}, "will miss target by 1.50 kWh"),
        (
            {"activations_today": 3, "activations_today_day_key": DAY},
            "deferrable",
            {"max_activations_per_day": 3},
            "capped (3/3)",
        ),
        (
            {"plan_target_kwh": 4.0, "delivered_today_kwh": 4.0},
            "deferrable",
            {},
            "done",
        ),
        (
            {"plan_target_kwh": 4.0, "delivered_today_kwh": 4.0},
            "deferrable",
            {"tank_current_temperature": 61.0, "done_condition_met": True},
            "done (tank 61 °C)",
        ),
        (
            {"plan_target_kwh": 4.0, "delivered_today_kwh": 4.0},
            "deferrable",
            {"tank_current_temperature": 52.0, "done_condition_met": False},
            "target met (4.0 of 4.0 kWh, tank 52 °C)",
        ),
        (
            {
                "plan_temperature_forecast": _plan([50, 55, 60]),
                "plan_deadline_period": 9,
            },
            "thermal",
            {},
            f"on track: tank projected 60 °C by {T0 + timedelta(hours=1):%H:%M}",
        ),
        (
            {"plan_forecast": _plan([0.5, 2.0]), "plan_nominal_kw": 2.0},
            "sheddable",
            {},
            "shed 0.75 kWh today",
        ),
        ({"plan_forecast": _plan([0, 0])}, "deferrable", {}, "outside window"),
    ]:
        s = replace(lrs.LoadRunState(), **state)
        assert (
            lrs.derive_schedule_view(s, load_kind=kind, now=T0, **kw).status == status
        )

    # test_money_saved_today_is_what_the_mean_price_would_have_cost_less_what_it_did
    s = replace(lrs.LoadRunState(), delivered_today_kwh=4.0, cost_today=0.5)
    assert (
        lrs.derive_schedule_view(
            s, load_kind="deferrable", now=T0, today_mean_import_price=0.3
        ).cost_avoided_today
        == 0.7
    )
    assert (
        lrs.derive_schedule_view(s, load_kind="deferrable", now=T0).cost_avoided_today
        is None
    )

    # test_an_unreadable_store_starts_the_load_afresh
    for store in [
        MemoryStore(broken=True),
        MemoryStore({"hw": {"delivered_today_kwh": "lots"}}),
        MemoryStore(None),
    ]:
        assert (
            asyncio.run(lrs.LoadRunStateStore(store=store).async_read("hw"))
            == lrs.LoadRunState()
        )
        asyncio.run(
            lrs.LoadRunStateStore(store=MemoryStore(broken=True)).async_write(
                "hw", lrs.LoadRunState()
            )
        )


# --- the debounced command --------------------------------------------------------------------


def _cmd(state, minutes, on, hysteresis=600):
    return lrs.decide_commanded_state(
        state,
        raw_new_state=on,
        now=T0 + timedelta(minutes=minutes),
        min_hysteresis_seconds=hysteresis,
    )


def _diverged(seconds_ago=1200.0, now=10_000.0, **kw):
    return replace(
        lrs.LoadRunState(),
        commanded_state=True,
        commanded_since=now - seconds_ago,
        currently_on=False,
        off_since=now - seconds_ago,
        **kw,
    )


# --- the schedule view ---------------------------------------------------------------------------------


def _plan(values, start=T0, minutes=30):
    return [
        {"time": (start + timedelta(minutes=minutes * i)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]


# --- kept across restarts -----------------------------------------------------------------------------------


class MemoryStore:
    def __init__(self, data=None, broken=False):
        self.data, self.broken = data, broken

    async def async_load(self):
        if self.broken:
            raise OSError("disk")
        return self.data

    async def async_save(self, data):
        self.data = data
