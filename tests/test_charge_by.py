"""Charge by (household, 14 Oct 2026): "we charge batteries while cheap to
sell for P2P". On 14 Oct every plan from 10:00 to 15:00 held a charge before
the 17:00 block, and every 15 s re-plan pushed it to a slot forecast a cent or
two cheaper, so it never started. "Charge By" / "Charge To" make the battery
hold a SoC by a time of day. Soft, so a target it cannot reach still solves
(charging as fast as it can) instead of making the plan infeasible. Real
HiGHS solves for the LP half.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
import pytest
import solver_shared
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import CHARGE_BY_SHORTFALL_PENALTY_PER_KWH, build_plan

CAP = 100.0
START = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)  # 10:00 AEST


def _plan(*, target_kwh=0.0, deadline=None, initial=24.0, max_charge=40.0, n=10):
    # 10:00-20:00 AEST, hourly. Buy 11c until 14:00, then 10c at 14:00-16:00
    # (the "cent cheaper later" slot), 30c after. Sell 45c 17:00-20:00.
    buy = np.array([0.11] * 4 + [0.10] * 2 + [0.30] * (n - 6))
    sell = np.array([0.0] * 7 + [0.45] * (n - 7))
    return build_plan(
        periods=PeriodGrid(hours=np.full(n, 1.0), start=START),
        grid=GridConfig(
            import_price=buy,
            export_price=sell,
            import_limit_kw=42.0,
            export_limit_kw=40.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=CAP,
                initial_soc_kwh=initial,
                min_soc_kwh=2.0,
                max_soc_kwh=CAP,
                max_charge_kw=max_charge,
                max_discharge_kw=40.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
                charge_by_kwh=target_kwh,
                charge_by_period_indices=deadline,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
    )


def test_without_it_the_plan_waits_for_the_cheaper_slot():
    plan = _plan()
    assert plan.status == "optimal"
    assert np.allclose(plan.battery_charge_kw[:4], 0.0, atol=1e-6)
    assert plan.battery_charge_kw[4:6].sum() > 30.0


def test_with_it_the_battery_holds_the_target_by_the_deadline():
    # deadline: the end of period 3 = 14:00 AEST, 80 kWh
    plan = _plan(target_kwh=80.0, deadline=frozenset({3}))
    assert plan.status == "optimal"
    assert plan.battery_soc_kwh[3] >= 80.0 - 1e-6
    assert plan.battery_charge_kw[:4].sum() > 40.0


def test_an_unreachable_target_still_solves_and_charges_flat_out():
    # 10 kW for 4 h from 24 kWh cannot reach 90 kWh by 14:00.
    plan = _plan(target_kwh=90.0, deadline=frozenset({3}), max_charge=10.0)
    assert plan.status == "optimal"
    assert np.allclose(plan.battery_charge_kw[:4], 10.0, atol=1e-6)
    assert plan.battery_soc_kwh[3] < 90.0


def test_off_when_the_target_is_zero():
    off = _plan(target_kwh=0.0, deadline=frozenset({3}))
    base = _plan()
    assert np.allclose(off.battery_charge_kw, base.battery_charge_kw, atol=1e-6)


def test_the_penalty_is_above_ordinary_prices_and_below_a_spike():
    assert 1.0 <= CHARGE_BY_SHORTFALL_PENALTY_PER_KWH <= 17.5


# --- the deadline in the plan's own tiered grid ----------------------------


def _grid(start, tiers):
    times, hours, t = [], [], start
    for count, minutes in tiers:
        for _ in range(count):
            times.append(t)
            hours.append(minutes / 60)
            t += timedelta(minutes=minutes)
    return times, hours


def _ends_local(times, hours, indices):
    return sorted(
        solver_shared._local(times[i] + timedelta(hours=hours[i])).strftime("%d %H:%M")
        for i in indices
    )


def test_the_last_period_ending_by_the_deadline_each_day():
    # 10:04 AEST: 1-min to 10:05, 5-min to 10:30, then 30-min on the clock
    times, hours = _grid(
        datetime(2026, 10, 14, 0, 4, tzinfo=UTC), [(1, 1), (5, 5), (140, 30)]
    )
    found = solver_shared.charge_by_period_indices(times, hours, 16.0)
    assert _ends_local(times, hours, found) == ["14 16:00", "15 16:00", "16 16:00"]


def test_a_deadline_inside_a_period_uses_the_period_before():
    times, hours = _grid(datetime(2026, 10, 14, 0, 0, tzinfo=UTC), [(20, 30)])
    found = solver_shared.charge_by_period_indices(times, hours, 16.25)
    assert _ends_local(times, hours, found) == ["14 16:00"]


def test_a_deadline_already_past_is_skipped():
    # 16:30 AEST: today's 16:00 is gone, tomorrow's is in the plan
    times, hours = _grid(datetime(2026, 10, 14, 6, 30, tzinfo=UTC), [(60, 30)])
    found = solver_shared.charge_by_period_indices(times, hours, 16.0)
    assert _ends_local(times, hours, found) == ["15 16:00"]


def test_a_deadline_beyond_the_plan_is_skipped():
    # 10:00-15:00 AEST: the plan never reaches 16:00
    times, hours = _grid(datetime(2026, 10, 14, 0, 0, tzinfo=UTC), [(10, 30)])
    assert solver_shared.charge_by_period_indices(times, hours, 16.0) == frozenset()


# --- what the plan publishes ------------------------------------------------


def _rows(socs):
    t = START
    out = []
    for soc in socs:
        out.append({"time": t.isoformat(), "hours": 1.0, "soc_pct": soc})
        t += timedelta(hours=1)
    return out


def test_summary_is_all_none_when_off():
    out = solver_shared.charge_by_summary(
        {"solver_charge_by_percent": 0}, _rows([50] * 8)
    )
    assert set(out) == {
        "charge_by_time",
        "charge_by_target_pct",
        "charge_by_next",
        "charge_by_next_soc_pct",
        "charge_by_next_short_pct",
    }
    assert all(v is None for v in out.values())


def test_summary_reports_the_next_deadline_and_the_shortfall():
    cfg = {"solver_charge_by_percent": 90, "solver_charge_by_hour": 14.5}
    # rows start 10:00 AEST hourly; the last one ending by 14:30 ends 14:00
    out = solver_shared.charge_by_summary(cfg, _rows([30, 45, 60, 75, 85, 95, 95, 95]))
    assert out["charge_by_time"] == "14:30"
    assert out["charge_by_target_pct"] == 90.0
    assert out["charge_by_next"].startswith("2026-10-14T14:00:00")
    assert out["charge_by_next_soc_pct"] == 75.0
    assert out["charge_by_next_short_pct"] == pytest.approx(15.0)


# --- the margin (household, 15 Oct 2026) -------------------------------------
# "I want it to meet its expected outcomes... not me to intervene." With the
# target at the deadline alone, the plan spent every minute of slack: on
# 15 Oct a Charge By of 14:00 / 100 % left the battery at ~95 % at 14:00 on
# the taper. The target now also binds CHARGE_BY_MARGIN_HOURS earlier.


def test_the_margin_is_one_hour():
    assert solver_shared.CHARGE_BY_MARGIN_HOURS == 1.0


def test_the_target_binds_at_the_deadline_and_an_hour_before_each_day():
    times, hours = _grid(
        datetime(2026, 10, 14, 0, 4, tzinfo=UTC), [(1, 1), (5, 5), (140, 30)]
    )
    found = solver_shared.charge_by_target_indices(times, hours, 15.0)
    assert _ends_local(times, hours, found) == [
        "14 14:00",
        "14 15:00",
        "15 14:00",
        "15 15:00",
        "16 14:00",
        "16 15:00",
    ]


def test_an_early_deadline_is_clamped_at_midnight():
    # 00:30: the margin would be 23:30 the day before; it binds at 00:00.
    times, hours = _grid(datetime(2026, 10, 14, 0, 0, tzinfo=UTC), [(96, 30)])
    found = solver_shared.charge_by_target_indices(times, hours, 0.5)
    assert _ends_local(times, hours, found) == [
        "15 00:00",
        "15 00:30",
        "16 00:00",
        "16 00:30",
    ]


def test_with_the_margin_the_target_is_held_an_hour_early():
    # Deadline 16:00 = end of period 5; the cheap 10c slots are 14:00-16:00,
    # so with the deadline alone the plan fills up in its last hour.
    alone = _plan(target_kwh=80.0, deadline=frozenset({5}))
    assert alone.status == "optimal"
    assert alone.battery_soc_kwh[5] >= 80.0 - 1e-6
    assert alone.battery_soc_kwh[4] < 80.0 - 1.0  # landed on the deadline
    # With the margin (end of period 4 = 15:00 too), it is full an hour early.
    early = _plan(target_kwh=80.0, deadline=frozenset({4, 5}))
    assert early.status == "optimal"
    assert early.battery_soc_kwh[4] >= 80.0 - 1e-6


def test_with_the_margin_an_unreachable_target_charges_at_once():
    # 10 kW cannot reach 90 kWh by 13:00 or 14:00: flat out from period 0.
    plan = _plan(target_kwh=90.0, deadline=frozenset({2, 3}), max_charge=10.0)
    assert plan.status == "optimal"
    assert np.allclose(plan.battery_charge_kw[:4], 10.0, atol=1e-6)


def test_the_plan_binds_the_target_with_the_margin():
    """The solve's battery gets charge_by_target_indices, not the deadline
    alone; without this pin the margin could be dropped at the call site."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "nimbus_load"
        / "solver_plan.py"
    ).read_text(encoding="utf-8")
    assert "charge_by_period_indices=charge_by_target_indices(" in src
    assert "charge_by_period_indices=charge_by_period_indices(" not in src
