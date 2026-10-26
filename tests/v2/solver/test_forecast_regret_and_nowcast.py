"""What a forecast cost: the hindsight-optimal plan, the plan made on the
forecast and the plan made on persistence, all scored against what really
happened -- and the same for the load nowcast (#919, #937) (#1837).

Properties on the reference day: hindsight is never beaten, a perfect
forecast costs nothing, and the value added is persistence's cost less the
forecast's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from solver.forecast_regret import compute_forecast_regret
from solver.nowcast_skill import (
    compute_load_nowcast_skill,
    forecast_value_at_lead_hours,
    period_sample_coverage,
)
from solver.reference_benchmark import build_reference_scenario


@pytest.fixture(scope="module")
def day():
    return build_reference_scenario()


@pytest.fixture(scope="module")
def regret(day):
    return compute_forecast_regret(**day)


def test_hindsight_is_never_beaten(regret):
    assert regret.j_star <= regret.j_forecast + 1e-6
    assert regret.j_star <= regret.j_persistence + 1e-6
    assert regret.forecast_regret_dollars == pytest.approx(
        regret.j_forecast - regret.j_star
    )
    assert regret.persistence_regret_dollars == pytest.approx(
        regret.j_persistence - regret.j_star
    )
    assert regret.nimbus_value_add_dollars == pytest.approx(
        regret.j_persistence - regret.j_forecast
    )


def _with_load(day, load=None, solar=None):
    return compute_forecast_regret(
        **dict(
            day,
            solar_forecast_kw=day["solar_real_kw"] if solar is None else solar,
            load_forecast_kw=load,
        )
    )


def test_a_biased_load_forecast_is_charged_to_level_a_late_one_to_shape(day):
    real = np.asarray(day["load_real_kw"], dtype=float)
    biased = _with_load(day, real * 1.30)
    assert biased.load_shape_error_dollars == pytest.approx(0.0, abs=1e-6)
    assert biased.load_level_error_dollars == pytest.approx(
        biased.forecast_regret_dollars, abs=1e-6
    )
    late = _with_load(day, np.roll(real, 3))  # the same total, three hours late
    assert late.load_level_error_dollars == pytest.approx(0.0, abs=1e-6)
    assert late.load_shape_error_dollars == pytest.approx(
        late.forecast_regret_dollars, abs=1e-6
    )
    both = _with_load(day, np.roll(real, 2) * 1.15)
    assert abs(both.load_level_error_dollars) > 1e-6
    assert abs(both.load_shape_error_dollars) > 1e-6
    assert both.solar_error_dollars == pytest.approx(0.0, abs=1e-6)


def _evening_day():
    """Solar by day and a load peak inside the dear evening: a day where
    timing costs money, so every term of the split can move."""
    from solver.elements import BatteryConfig, GridConfig, PeriodGrid

    hour = np.arange(24)
    solar = np.clip(8.0 * np.sin((hour - 6.0) / 12.0 * np.pi), 0.0, None)
    evening = (hour >= 17) & (hour <= 21)
    load = 1.5 + np.sin(hour / 24.0 * 2 * np.pi) + 2.0 * evening
    return {
        "periods": PeriodGrid(
            hours=np.ones(24), start=datetime(2026, 9, 16, tzinfo=UTC)
        ),
        "grid": GridConfig(
            import_price=np.where(evening, 0.90, 0.20),
            export_price=np.where(evening, 0.40, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        "battery": BatteryConfig(
            name="battery",
            capacity_kwh=20.0,
            initial_soc_kwh=10.0,
            min_soc_kwh=1.0,
            max_soc_kwh=20.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.005,
            discharge_cost=0.01,
            salvage_value=0.15,
        ),
        "solar_real_kw": solar,
        "load_real_kw": load,
        "solar_persistence_kw": np.roll(solar, 3) * 0.85,
        "load_persistence_kw": np.roll(load, 2) * 1.1,
    }


def test_a_zero_load_forecast_has_no_split_but_still_has_a_cost(day):
    r = _with_load(day, np.zeros(len(day["load_real_kw"])))
    assert (
        r.load_level_error_dollars,
        r.load_shape_error_dollars,
        r.solar_error_dollars,
    ) == (None, None, None)
    assert isinstance(r.nimbus_value_add_dollars, float)
    assert r.forecast_regret_dollars >= -1e-6


# --- the load nowcast ---------------------------------------------------------------------


@pytest.mark.parametrize(("measured", "short"), [(5, False), (24, True)])
def test_too_little_measured_or_misaligned_input_is_no_score(day, measured, short):
    load = day["load_real_kw"][:-1] if short else day["load_real_kw"]
    assert (
        compute_load_nowcast_skill(
            periods=day["periods"],
            grid=day["grid"],
            battery=day["battery"],
            solar_real_kw=day["solar_real_kw"],
            load_real_kw=day["load_real_kw"],
            load_nowcast_kw=load,
            load_persistence_kw=day["load_persistence_kw"],
            n_periods_measured=measured,
        )
        is None
    )


T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


def test_a_period_counts_as_measured_when_a_sample_falls_inside_it():
    grid = [T0 + timedelta(minutes=30 * i) for i in range(4)]
    samples = [
        T0 + timedelta(minutes=5),
        T0 + timedelta(minutes=30),
        T0 + timedelta(minutes=125),
    ]
    assert period_sample_coverage(samples, grid, 0.5) == (2, 4)
    assert period_sample_coverage(samples, [], 0.5) == (0, 0)


def test_the_forecast_made_lead_hours_ahead_is_read_off_its_grid():
    for lead, value in [(0.0, 1.0), (0.75, 2.0), (1.5, 4.0), (2.0, None), (-1.0, None)]:
        grid = [T0 + timedelta(minutes=30 * i) for i in range(4)]
        assert (
            forecast_value_at_lead_hours(grid, np.array([1.0, 2.0, 3.0, 4.0]), T0, lead)
            == value
        )
        assert forecast_value_at_lead_hours(grid, np.array([1.0]), T0, 0.0) is None
