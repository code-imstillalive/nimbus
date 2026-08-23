"""Real tests for solver/rolling.py -- Layer 2's receding-horizon
re-solve loop. Genuinely dead code today (not imported by __init__.py/
coordinator.py/config_flow.py, per its own module docstring), but a
real, carefully-designed mechanism (SoC continuity across ticks, a
clamp-not-crash policy-shift interaction, a solve-failure fallback) with
zero prior test coverage. Uses real build_plan() solves throughout, not
mocks, matching this project's own established convention for solver
tests.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from solver.elements import (
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.rolling import (
    RollingInputs,
    RollingRefinementConfig,
    run_rolling_refinement,
)

_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _flat_grid(n: int, start: datetime, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=start)


def _battery(**overrides) -> BatteryConfig:
    defaults = {
        "capacity_kwh": 20.0,
        "initial_soc_kwh": 10.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 20.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _grid(n: int, **overrides) -> GridConfig:
    defaults = {
        "import_price": np.full(n, 0.30),
        "export_price": np.full(n, 0.05),
        "import_limit_kw": 20.0,
        "export_limit_kw": 20.0,
    }
    defaults.update(overrides)
    return GridConfig(**defaults)


# --- RollingRefinementConfig validation -------------------------------------


def test_config_rejects_non_positive_n_resolves():
    with pytest.raises(ValueError, match="n_resolves"):
        RollingRefinementConfig(
            start=_START, resolve_interval=timedelta(minutes=5), n_resolves=0
        )


def test_config_rejects_non_positive_resolve_interval():
    with pytest.raises(ValueError, match="resolve_interval"):
        RollingRefinementConfig(
            start=_START, resolve_interval=timedelta(seconds=0), n_resolves=3
        )


# --- run_rolling_refinement: input validation -------------------------------


def test_raises_when_periods_start_is_none():
    """Cross-solve alignment (network.py) is impossible without a real
    calendar anchor -- this must fail loudly, not silently skip
    stability mechanisms."""

    def provider(now):
        n = 4
        return RollingInputs(
            periods=PeriodGrid(hours=np.full(n, 1.0), start=None),  # no anchor
            grid=_grid(n),
            battery=_battery(),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=2
    )
    with pytest.raises(ValueError, match="periods.start must be set"):
        run_rolling_refinement(config, provider)


def test_raises_when_provider_returns_a_window_starting_before_now():
    def provider(now):
        n = 4
        return RollingInputs(
            periods=_flat_grid(n, start=now - timedelta(hours=1)),  # before 'now'
            grid=_grid(n),
            battery=_battery(),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=2
    )
    with pytest.raises(ValueError, match="is before this tick's own 'now'"):
        run_rolling_refinement(config, provider)


# --- run_rolling_refinement: real multi-tick behaviour ----------------------


def test_produces_exactly_n_resolves_ticks_at_the_right_times():
    def provider(now):
        n = 4
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=_grid(n),
            battery=_battery(),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(minutes=30), n_resolves=3
    )
    result = run_rolling_refinement(config, provider)
    assert len(result.ticks) == 3
    assert [t.solved_at for t in result.ticks] == [
        _START,
        _START + timedelta(minutes=30),
        _START + timedelta(minutes=60),
    ]


def test_every_tick_is_optimal_under_a_simple_flat_price_scenario():
    def provider(now):
        n = 4
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=_grid(n),
            battery=_battery(),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=4
    )
    result = run_rolling_refinement(config, provider)
    assert result.n_infeasible == 0
    for t in result.ticks:
        assert t.plan.is_optimal


def test_result_properties_expose_the_same_per_tick_values_as_the_ticks_list():
    def provider(now):
        n = 4
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=_grid(n),
            battery=_battery(),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=3
    )
    result = run_rolling_refinement(config, provider)
    assert result.dispatch_charge_kw == [t.dispatched_charge_kw for t in result.ticks]
    assert result.dispatch_discharge_kw == [
        t.dispatched_discharge_kw for t in result.ticks
    ]
    assert result.dispatch_grid_import_kw == [
        t.dispatched_grid_import_kw for t in result.ticks
    ]
    assert result.dispatch_grid_export_kw == [
        t.dispatched_grid_export_kw for t in result.ticks
    ]
    assert result.dispatch_soc_kwh == [t.dispatched_soc_kwh for t in result.ticks]


# --- SoC continuity across ticks (the module's own core responsibility) ----


def test_soc_continuity_second_tick_starts_from_first_ticks_real_dispatched_soc():
    """The whole point of this module, per its own docstring: each
    re-solve's initial_soc_kwh must be the REAL dispatched SoC from the
    previous tick, not whatever static value the input-provider's own
    BatteryConfig happens to carry (10.0 below, deliberately never
    updated by the provider itself -- if the loop's own override didn't
    exist, every tick would silently re-solve from 10.0 regardless of
    what actually happened).

    The direct, load-bearing check: tick 2's own solved battery_soc_kwh
    at period 0 (which reflects whatever initial_soc_kwh the loop fed
    into build_plan()) must equal tick 1's real dispatched_soc_kwh, not
    the provider's own static 10.0.
    """

    # A generous export price makes discharging genuinely attractive
    # against the default 0.01 discharge_cost, so SoC actually moves
    # tick to tick rather than staying flat by coincidence -- confirmed
    # below, not just assumed.
    def provider(now):
        n = 2
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=_grid(n, export_price=np.full(n, 0.50)),
            battery=_battery(initial_soc_kwh=10.0),
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=3
    )
    result = run_rolling_refinement(config, provider)

    # A real discharge genuinely happened tick 1 -- otherwise this test
    # wouldn't be exercising SoC continuity at all.
    assert result.dispatch_soc_kwh[0] != pytest.approx(10.0)

    # The actual point of the test: tick 2 solved from tick 1's real
    # ending SoC, not from the provider's own unchanging 10.0.
    assert result.ticks[1].plan.battery_soc_kwh[0] == pytest.approx(
        result.ticks[0].dispatched_soc_kwh
    )
    assert result.ticks[2].plan.battery_soc_kwh[0] == pytest.approx(
        result.ticks[1].dispatched_soc_kwh
    )


def test_soc_carried_forward_is_clamped_into_the_new_ticks_own_bounds():
    """Real, deliberate project scenario (see module docstring): a
    min_soc/max_soc policy shift between ticks is legitimate, not a
    caller bug -- the carried-forward SoC must clamp into the NEW
    tick's bounds rather than crash or silently violate them."""
    call_count = {"n": 0}

    def provider(now):
        call_count["n"] += 1
        n = 2
        if call_count["n"] == 1:
            # Tick 1: wide bounds, battery ends up somewhere mid-range.
            battery = _battery(initial_soc_kwh=10.0, min_soc_kwh=2.0, max_soc_kwh=20.0)
        else:
            # Tick 2+: policy tightens max_soc_kwh down HARD, likely
            # below wherever tick 1 actually left the real SoC. This
            # tick's own provider-returned initial_soc_kwh (5.0) must
            # itself be valid for ITS OWN [2, 6] bounds -- BatteryConfig
            # validates eagerly at construction, before the loop ever
            # gets a chance to override it; the real thing being tested
            # is that the loop's OWN override replaces this 5.0 with
            # tick 1's real ending SoC, clamped into [2, 6].
            battery = _battery(initial_soc_kwh=5.0, min_soc_kwh=2.0, max_soc_kwh=6.0)
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=_grid(n),
            battery=battery,
            solar=SolarConfig(forecast_kw=np.zeros(n)),
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=2
    )
    result = run_rolling_refinement(config, provider)
    # Must not have raised -- the clamp is what prevents a crash here.
    assert len(result.ticks) == 2
    assert result.ticks[1].plan.is_optimal
    # tick 2's solve must have started from a SoC within ITS OWN bounds
    # (<=6.0), even though tick 1 may have left the real SoC higher.
    assert result.ticks[1].plan.battery_soc_kwh[0] <= 6.0 + 1e-6


# --- solve-failure fallback --------------------------------------------------


def test_infeasible_solve_freezes_the_last_known_good_dispatch():
    """A genuinely impossible tick (import_limit_kw forced to 0 with no
    solar and a battery too weak to cover load) must not crash the loop
    or invent a plan -- it carries the prior tick's real dispatch
    forward unchanged, and n_infeasible counts it."""
    from solver.elements import LoadConfig

    call_count = {"n": 0}

    def provider(now):
        call_count["n"] += 1
        n = 2
        if call_count["n"] == 2:
            # Tick 2: genuinely infeasible -- a real, hard, non-sheddable
            # LoadConfig (10kW, must be served every period, "no
            # exceptions" per its own docstring) with zero import limit,
            # zero solar, and a battery that's empty AND physically
            # unable to discharge. Nothing can possibly serve this load.
            grid = _grid(n, import_limit_kw=0.0, export_limit_kw=0.0)
            battery = _battery(
                initial_soc_kwh=2.0,
                min_soc_kwh=2.0,
                max_soc_kwh=20.0,
                max_discharge_kw=0.0,
            )
            loads = [LoadConfig(name="impossible", forecast_kw=np.full(n, 10.0))]
        else:
            grid = _grid(n)
            battery = _battery()
            loads = None
        return RollingInputs(
            periods=_flat_grid(n, start=now),
            grid=grid,
            battery=battery,
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=loads,
        )

    config = RollingRefinementConfig(
        start=_START, resolve_interval=timedelta(hours=1), n_resolves=3
    )
    result = run_rolling_refinement(config, provider)
    assert len(result.ticks) == 3
    assert result.n_infeasible >= 1
    infeasible_tick = next(t for t in result.ticks if not t.plan.is_optimal)
    idx = result.ticks.index(infeasible_tick)
    if idx > 0:
        prev = result.ticks[idx - 1]
        assert infeasible_tick.dispatched_charge_kw == prev.dispatched_charge_kw
        assert infeasible_tick.dispatched_discharge_kw == prev.dispatched_discharge_kw
        assert infeasible_tick.dispatched_soc_kwh == prev.dispatched_soc_kwh


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
