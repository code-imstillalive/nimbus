"""Household, 20 Oct 2026: "Don't sell below" -- stop selling spare solar
cheaply while the battery has room.

The morning shape: spare solar was exported at a low price while the battery
sat at its reserve, because the plan saw grid energy later that day as about
as cheap, and because period 0 was one minute long, so holding the battery
idle cost under a cent and the direction-change margin kept it idle solve
after solve. The household asked for a threshold, not an override: below it
the plan should store rather than sell; above it, decide on price as before;
a full battery still exports.

Synthetic data only. The LP tests are real HiGHS solves.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solver import network
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

from custom_components.nimbus_load import solver_plan, solver_publish

START = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)  # 08:00 AEST
N = 12
HOURS = np.full(N, 0.5)
# Two sunny hours, two hours of cheaper grid with no sun, then an evening.
SOLAR = np.array([8.0] * 4 + [0.0] * 8)
LOAD = np.full(N, 3.0)
EXPORT = np.array([0.09] * 4 + [0.06] * 2 + [0.30] * 6)
IMPORT = np.array([0.16] * 4 + [0.084] * 2 + [0.40] * 6)


# Room for 10 kWh: enough for the morning's spare solar OR the cheap grid, not
# both, so where the battery is filled from is the plan's real choice.
CAPACITY = 15.0


def _battery(initial_soc=5.0, **kw):
    return BatteryConfig(
        name="home",
        capacity_kwh=CAPACITY,
        initial_soc_kwh=initial_soc,
        min_soc_kwh=2.0,
        max_soc_kwh=CAPACITY,
        max_charge_kw=36.0,
        max_discharge_kw=36.0,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.0,
        discharge_cost=0.01,
        salvage_value=0.0,
        **kw,
    )


def _plan(
    floor=0.0,
    battery=None,
    export=EXPORT,
    hours=HOURS,
    solar=SOLAR,
    load=LOAD,
    imp=IMPORT,
):
    return build_plan(
        periods=PeriodGrid(hours=np.asarray(hours, dtype=float), start=START),
        grid=GridConfig(
            import_price=np.asarray(imp, dtype=float),
            export_price=np.asarray(export, dtype=float),
            import_limit_kw=60.0,
            export_limit_kw=40.0,
            export_floor_price=floor,
        ),
        batteries=[battery or _battery()],
        solar=SolarConfig(forecast_kw=np.asarray(solar, dtype=float)),
        loads=[LoadConfig(name="house", forecast_kw=np.asarray(load, dtype=float))],
    )


def _sunny_export_kwh(plan) -> float:
    return float(np.sum(plan.grid_export_kw[:4] * HOURS[:4]))


def _sunny_charge_kwh(plan) -> float:
    return float(np.sum(plan.batteries[0].charge_kw[:4] * HOURS[:4]))


class TestThePlan:
    def test_without_a_floor_spare_solar_is_sold_and_the_grid_fills_it_later(self):
        # The control: export at 9c now, import at 8.4c later -- on price
        # alone, selling the solar and buying back from the grid wins.
        plan = _plan()
        assert plan.status == "optimal"
        assert _sunny_export_kwh(plan) > 9.0
        assert float(np.sum(plan.grid_import_kw[4:6] * HOURS[4:6])) > 9.0

    def test_below_the_floor_spare_solar_fills_the_battery_instead(self):
        plan = _plan(floor=0.15)
        assert plan.status == "optimal"
        assert _sunny_export_kwh(plan) < 1e-6
        # All 5 kW of spare solar, four half-hours.
        assert _sunny_charge_kwh(plan) >= 10.0 - 1e-6
        assert float(np.sum(plan.solar_curtailed_kw)) < 1e-6

    def test_a_full_battery_still_exports_and_curtails_nothing(self):
        plan = _plan(floor=0.15, battery=_battery(initial_soc=CAPACITY))
        assert plan.status == "optimal"
        np.testing.assert_allclose(plan.grid_export_kw[:4], 5.0, atol=1e-6)
        assert float(np.sum(plan.solar_curtailed_kw)) < 1e-6

    def test_at_or_above_the_floor_the_plan_decides_on_price_as_before(self):
        high = EXPORT.copy()
        high[:4] = 0.15  # exactly the floor: not below it
        base = _plan(export=high)
        floored = _plan(floor=0.15, export=high)
        np.testing.assert_allclose(base.grid_export_kw, floored.grid_export_kw)
        np.testing.assert_allclose(
            base.batteries[0].soc_kwh, floored.batteries[0].soc_kwh
        )
        assert floored.export_floor_penalty_cost == 0.0

    def test_off_is_byte_identical(self):
        base = _plan()
        off = _plan(floor=0.0)
        np.testing.assert_array_equal(base.grid_export_kw, off.grid_export_kw)
        np.testing.assert_array_equal(
            base.batteries[0].soc_kwh, off.batteries[0].soc_kwh
        )
        assert base.total_cost == off.total_cost
        assert off.export_floor_penalty_cost == 0.0

    def test_the_evening_export_above_the_floor_is_untouched(self):
        # Evening export at 30c is above the floor: the full battery is sold
        # into it exactly as without a floor.
        plan = _plan(floor=0.15)
        base = _plan()
        assert float(np.sum(plan.grid_export_kw[6:] * HOURS[6:])) > 1.0
        np.testing.assert_allclose(
            plan.grid_export_kw[6:], base.grid_export_kw[6:], atol=1e-6
        )

    def test_the_penalty_is_counted_apart_from_the_plan_cost(self):
        # The published plan cost leaves the floor's penalty out (it is a
        # preference, not money), and the breakdown reconciles to that cost
        # with no penalty line in it.
        full = _plan(floor=0.15, battery=_battery(initial_soc=CAPACITY))
        rates = network.export_floor_penalty_rates(full.effective_export_price, 0.15)
        expected = float(np.sum(rates * full.grid_export_kw * HOURS))
        assert expected > 0
        assert full.export_floor_penalty_cost == pytest.approx(expected, rel=1e-9)
        plan_cost = full.total_cost - full.export_floor_penalty_cost
        breakdown = solver_publish.compute_cost_breakdown(
            net_costs=[0.0] * N,
            total_cost=plan_cost,
            degradation_cost_per_kwh=0.0,
            total_throughput_kwh=0.0,
            charge_cost=0.0,
            total_charge_kwh=0.0,
            discharge_cost_arr=np.zeros(N),
            battery_discharge_kw=np.zeros(N),
            period_hours=HOURS,
        )
        assert "export_floor_penalty" not in breakdown
        assert sum(breakdown.values()) == pytest.approx(plan_cost, abs=1e-3)

    def test_a_committed_p2p_export_carries_no_penalty(self):
        # A P2P block's fixed rate below the floor: the floor cannot change
        # it, so it is not charged, and the plan is the floor-off plan.
        fixed = np.zeros(N)
        fixed[6:9] = 5.0  # a 5 kW commitment, priced below the floor
        export = EXPORT.copy()
        export[6:9] = 0.10

        def solve(floor):
            return build_plan(
                periods=PeriodGrid(hours=HOURS, start=START),
                grid=GridConfig(
                    import_price=IMPORT.copy(),
                    export_price=export,
                    import_limit_kw=60.0,
                    export_limit_kw=40.0,
                    fixed_export_kw=fixed,
                    export_floor_price=floor,
                ),
                batteries=[_battery(initial_soc=CAPACITY)],
                solar=SolarConfig(forecast_kw=np.zeros(N)),
                loads=[LoadConfig(name="house", forecast_kw=LOAD.copy())],
            )

        floored, base = solve(0.15), solve(0.0)
        assert floored.status == base.status == "optimal"
        np.testing.assert_allclose(floored.grid_export_kw[6:9], 5.0, atol=1e-6)
        assert floored.export_floor_penalty_cost == pytest.approx(0.0, abs=1e-9)
        np.testing.assert_allclose(
            floored.batteries[0].soc_kwh, base.batteries[0].soc_kwh, atol=1e-6
        )

    def test_a_negative_floor_is_refused(self):
        with pytest.raises(ValueError, match="export_floor_price"):
            GridConfig(
                import_price=np.zeros(1),
                export_price=np.zeros(1),
                import_limit_kw=1.0,
                export_limit_kw=1.0,
                export_floor_price=-0.01,
            )


class TestThePenaltyRates:
    def test_below_the_floor_an_export_keeps_only_the_residual(self):
        rates = network.export_floor_penalty_rates(np.array([0.09, 0.15, 0.20]), 0.15)
        res = network.EXPORT_FLOOR_RESIDUAL_VALUE
        np.testing.assert_allclose(rates, [0.09 - res, 0.0, 0.0])

    def test_a_price_at_or_below_the_residual_is_not_penalised(self):
        res = network.EXPORT_FLOOR_RESIDUAL_VALUE
        rates = network.export_floor_penalty_rates(np.array([-0.05, 0.0, res]), 0.15)
        np.testing.assert_array_equal(rates, [0.0, 0.0, 0.0])

    def test_off_is_all_zero(self):
        rates = network.export_floor_penalty_rates(np.array([0.01, 0.09]), 0.0)
        np.testing.assert_array_equal(rates, [0.0, 0.0])


# ---- the direction-change margin ------------------------------------------
# 20 Oct: period 0 was one minute. The free plan charged from spare solar;
# the pinned "stay idle" plan exported it instead and cost under a cent
# more, so a 1c margin kept the battery idle every solve.

ONE_MINUTE_HOURS = np.array([1 / 60] + [0.5] * 6)
ONE_MINUTE_SOLAR = np.array([9.0] + [0.0] * 6)  # a forecast that sees no more sun
ONE_MINUTE_LOAD = np.full(7, 3.0)
ONE_MINUTE_EXPORT = np.array([0.077] + [0.06] * 2 + [0.30] * 4)
ONE_MINUTE_IMPORT = np.array([0.16] + [0.084] * 2 + [0.40] * 4)


def _one_minute(floor, pin_kw=None):
    battery = _battery(initial_soc=5.0)
    if pin_kw is not None:
        battery = dataclasses.replace(battery, period0_pin_net_kw=pin_kw)
    return _plan(
        floor=floor,
        battery=battery,
        export=ONE_MINUTE_EXPORT,
        hours=ONE_MINUTE_HOURS,
        solar=ONE_MINUTE_SOLAR,
        load=ONE_MINUTE_LOAD,
        imp=ONE_MINUTE_IMPORT,
    )


def _hysteresis(monkeypatch, floor):
    # The previous plan held the battery idle at period 0.
    monkeypatch.setattr(solver_plan, "_anchor_target_kw", lambda plan, prev: (0, 0.0))
    free = _one_minute(floor)
    return free, solver_plan.period0_direction_hysteresis(
        free,
        object(),
        lambda kw: _one_minute(floor, pin_kw=kw),
        0.01,
        export_floor=floor,
    )


def test_the_free_plan_charges_from_the_one_minute_of_sun(monkeypatch):
    free = _one_minute(0.15)
    assert free.status == "optimal"
    assert float(free.batteries[0].charge_kw[0]) > 5.0
    assert float(free.grid_export_kw[0]) < 1e-6


def test_without_the_floor_the_margin_keeps_the_battery_idle(monkeypatch):
    # The control, the 20 Oct shape: the switch is worth under a cent.
    _free, out = _hysteresis(monkeypatch, 0.0)
    assert out.period0_hysteresis == "kept"
    assert float(out.grid_export_kw[0]) > 5.0


def test_with_the_floor_the_margin_does_not_keep_a_cheap_export(monkeypatch):
    free, out = _hysteresis(monkeypatch, 0.15)
    assert out.period0_hysteresis == "export_floor"
    # The free plan, charging, is what is published.
    np.testing.assert_array_equal(
        out.batteries[0].charge_kw, free.batteries[0].charge_kw
    )
    assert float(out.grid_export_kw[0]) < 1e-6
    assert out.period0_hysteresis_delta is not None
    assert out.period0_hysteresis_delta <= 0.01


def test_above_the_floor_the_margin_works_as_before(monkeypatch):
    # Period 0 priced at 20c, above a 15c floor: the floor has no say.
    monkeypatch.setattr(solver_plan, "_anchor_target_kw", lambda plan, prev: (0, 0.0))
    export = ONE_MINUTE_EXPORT.copy()
    export[0] = 0.20

    def solve(pin_kw=None):
        battery = _battery(initial_soc=5.0)
        if pin_kw is not None:
            battery = dataclasses.replace(battery, period0_pin_net_kw=pin_kw)
        return _plan(
            floor=0.15,
            battery=battery,
            export=export,
            hours=ONE_MINUTE_HOURS,
            solar=ONE_MINUTE_SOLAR,
            load=ONE_MINUTE_LOAD,
            imp=ONE_MINUTE_IMPORT,
        )

    free = solve()
    out = solver_plan.period0_direction_hysteresis(
        free, object(), lambda kw: solve(pin_kw=kw), 0.01, export_floor=0.15
    )
    assert out.period0_hysteresis != "export_floor"
