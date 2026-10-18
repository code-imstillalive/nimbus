"""nimbus #1654, options A and C: the plan flipped at the reserve floor.

18 Oct 2026, 05:25-06:56 AEST on the reference household: SoC sat a few
tenths above the 10% soft reserve, the current 5-minute interval was priced
below the next ones (15.7c against 18.5-19.3c), and consecutive solves
alternated between discharging ~0.85 kW and holding. Each flip was worth a
fraction of a cent, and the inverter got 26 mode commands.

A: keep the previous period-0 direction unless switching saves more than
`solver_period0_hysteresis` ($). C: the reserve's own dual is published apart
from the energy price.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import itertools

from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

from custom_components.nimbus_load import solver_plan, solver_publish

START = datetime(2026, 10, 17, 19, 25, tzinfo=UTC)  # 05:25 AEST
N = 12  # one hour of 5-minute periods


def _plan(net_kw: float, cost: float, status: str = "optimal"):
    """A stand-in plan with only what the hysteresis reads."""
    return SimpleNamespace(
        status=status,
        total_cost=cost,
        battery_discharge_kw=np.array([max(net_kw, 0.0)]),
        battery_charge_kw=np.array([max(-net_kw, 0.0)]),
        periods=PeriodGrid(np.full(1, 5 / 60), start=START),
    )


def _with_anchor(monkeypatch, kw):
    monkeypatch.setattr(solver_plan, "_anchor_target_kw", lambda plan, prev: (0, kw))


def _dc(ns):
    """dataclasses.replace needs a dataclass: wrap the stand-in."""

    @dataclasses.dataclass
    class P:
        status: str
        total_cost: float
        battery_discharge_kw: np.ndarray
        battery_charge_kw: np.ndarray
        periods: PeriodGrid
        period0_hysteresis: str = "off"
        period0_hysteresis_delta: float | None = None

    return P(
        ns.status,
        ns.total_cost,
        ns.battery_discharge_kw,
        ns.battery_charge_kw,
        ns.periods,
    )


def test_a_cheap_switch_keeps_the_previous_direction(monkeypatch):
    _with_anchor(monkeypatch, 0.855)
    free = _dc(_plan(0.0, -73.4524))
    pinned = _dc(_plan(0.855, -73.4458))
    out = solver_plan.period0_direction_hysteresis(
        free, object(), lambda kw: pinned, 0.01
    )
    assert out.period0_hysteresis == "kept"
    assert out.battery_discharge_kw[0] == 0.855
    assert out.period0_hysteresis_delta == 0.0066


def test_a_switch_worth_more_than_the_margin_goes_ahead(monkeypatch):
    _with_anchor(monkeypatch, 0.855)
    free = _dc(_plan(0.0, -73.50))
    pinned = _dc(_plan(0.855, -73.40))
    out = solver_plan.period0_direction_hysteresis(
        free, object(), lambda kw: pinned, 0.01
    )
    assert out.period0_hysteresis == "switched"
    assert out.battery_discharge_kw[0] == 0.0
    assert out.period0_hysteresis_delta == 0.1


def test_an_infeasible_pin_switches(monkeypatch):
    _with_anchor(monkeypatch, 0.855)
    free = _dc(_plan(0.0, -73.50))
    pinned = _dc(_plan(0.0, None, status="infeasible"))
    out = solver_plan.period0_direction_hysteresis(
        free, object(), lambda kw: pinned, 0.01
    )
    assert out.period0_hysteresis == "switched"


def test_same_direction_and_off_and_no_previous_solve_nothing(monkeypatch):
    calls = []

    def pin(kw):
        calls.append(kw)
        raise AssertionError("no re-solve expected")

    _with_anchor(monkeypatch, 0.60)
    free = _dc(_plan(0.85, -1.0))
    assert (
        solver_plan.period0_direction_hysteresis(
            free, object(), pin, 0.01
        ).period0_hysteresis
        == "same_direction"
    )
    assert (
        solver_plan.period0_direction_hysteresis(
            free, object(), pin, 0.0
        ).period0_hysteresis
        == "off"
    )
    assert (
        solver_plan.period0_direction_hysteresis(
            free, None, pin, 0.01
        ).period0_hysteresis
        == "no_previous"
    )
    assert calls == []


def test_a_raising_re_solve_never_fails_the_solve(monkeypatch):
    _with_anchor(monkeypatch, 0.855)
    free = _dc(_plan(0.0, -1.0))

    def boom(kw):
        raise RuntimeError("HiGHS")

    out = solver_plan.period0_direction_hysteresis(free, object(), boom, 0.01)
    assert out.period0_hysteresis == "switched"
    assert out.battery_discharge_kw[0] == 0.0


# --- The real LP: the 18 Oct shape, solve after solve ----------------------


def _site(start, cheap_now: bool, soc_pct: float, prev=None, pin=None):
    """SoC a few tenths above a 10% reserve on 119.7 kWh, ~0.85 kW house
    load, the reserve released at 06:00 (period 6). The current interval
    costs 15.7c when `cheap_now`, else 18.6c; the rest of the window 18.5c,
    after it 18.6-19.3c. The energy above the reserve covers about three of
    the window's six periods, so the plan spends it in the dearest ones."""
    cap = 119.7
    hours = np.full(N, 5 / 60)
    buy = np.full(N, 0.185)
    buy[6:] = np.linspace(0.186, 0.193, N - 6)
    buy[0] = 0.157 if cheap_now else 0.186
    battery = BatteryConfig(
        name="home",
        capacity_kwh=cap,
        initial_soc_kwh=cap * soc_pct / 100,
        min_soc_kwh=0.02 * cap,
        max_soc_kwh=cap,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.05,
        salvage_value=0.10,
        reserve_kwh=0.10 * cap,
        reserve_release_price=0.30,
        reserve_period_indices=frozenset(range(6)),  # released at 06:00
        period0_pin_net_kw=pin,
    )
    return {
        "periods": PeriodGrid(hours, start=start),
        "grid": GridConfig(
            import_price=buy,
            export_price=buy - 0.12,
            import_limit_kw=60.0,
            export_limit_kw=0.0,
        ),
        "batteries": [battery],
        "solar": SolarConfig(forecast_kw=np.zeros(N)),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(N, 0.85))],
        "previous_plan": prev,
        # Off, so the test measures the hysteresis alone. Production runs a
        # proximal weight and still flipped 22 times that morning.
        "proximal_weight": 0.0,
    }


def _sequence(margin: float) -> list[int]:
    """Eight solves 30 s apart, the current interval's price alternating as
    it did that morning. Returns each published plan's period-0 class."""
    prev = None
    classes = []
    soc = 10.21
    for i in range(8):
        start = START + timedelta(seconds=30 * i)
        cheap = i % 2 == 0
        free = build_plan(**_site(start, cheap, soc, prev=prev))
        assert free.status == "optimal"
        plan = solver_plan.period0_direction_hysteresis(
            free,
            prev,
            lambda kw, start=start, cheap=cheap, prev=prev: build_plan(
                **_site(start, cheap, soc, prev=prev, pin=kw)
            ),
            margin,
        )
        classes.append(solver_plan._deadband_class(solver_plan._period0_net_kw(plan)))
        prev = plan
    return classes


def _changes(classes: list[int]) -> int:
    return sum(a != b for a, b in itertools.pairwise(classes))


def test_without_hysteresis_the_plan_flips_with_the_current_price():
    """The reproduction: off, consecutive solves change direction."""
    assert _changes(_sequence(0.0)) >= 3


def test_with_a_one_cent_margin_it_holds_its_direction():
    assert _changes(_sequence(0.01)) <= 1


# --- C: the reserve's dual, apart from the energy price ---------------------


def test_the_reserve_rows_are_named_and_reported_when_they_bind():
    plan = build_plan(**_site(START, True, 10.05))
    assert any(k.startswith("battery_reserve_floor_home_") for k in plan.duals)
    out = solver_publish._reserve_shadow_now(plan)
    assert set(out) == {"reserve_binding_now", "reserve_shadow_price_now"}
    assert out["reserve_binding_now"] is True
    assert out["reserve_shadow_price_now"] >= 0.0


def test_no_reserve_reports_nothing_binding():
    out = solver_publish._reserve_shadow_now(
        SimpleNamespace(duals={"power_balance_t0": 0.02})
    )
    assert out == {"reserve_binding_now": False, "reserve_shadow_price_now": 0.0}
