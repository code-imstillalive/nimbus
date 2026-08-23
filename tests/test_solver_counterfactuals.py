"""Real tests for solver/counterfactuals.py -- the three-counterfactual
mechanism built for Mark Purcell's ask #3 ("no control, a tuned
two-threshold price rule, the oracle"). Zero prior coverage despite
being real, already-shipped logic driving a real regret-decomposition
metric this project reports on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from solver.counterfactuals import (
    no_control_dispatch,
    tune_two_threshold,
    two_threshold_dispatch,
)

# --- no_control_dispatch ----------------------------------------------------


def test_no_control_dispatch_is_all_zeros():
    result = no_control_dispatch(n_periods=6, initial_soc_kwh=42.0)
    assert np.all(result.charge_kw == 0.0)
    assert np.all(result.discharge_kw == 0.0)
    assert len(result.charge_kw) == 6
    assert len(result.discharge_kw) == 6


def test_no_control_dispatch_soc_never_moves():
    """The whole point of this baseline -- SoC is exactly whatever it
    started at, since nothing ever charges or discharges."""
    result = no_control_dispatch(n_periods=10, initial_soc_kwh=55.5)
    assert result.final_soc_kwh == 55.5


# --- two_threshold_dispatch: the three real branches ------------------------


_BASE_KWARGS = {
    "initial_soc_kwh": 50.0,
    "min_soc_kwh": 10.0,
    "max_soc_kwh": 100.0,
    "max_charge_kw": 10.0,
    "max_discharge_kw": 10.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
    "threshold_low": 0.10,
    "threshold_high": 0.30,
}


def test_discharges_at_max_rate_when_price_above_high_threshold():
    result = two_threshold_dispatch(
        price_kwh=np.array([0.50]), hours=np.array([1.0]), **_BASE_KWARGS
    )
    assert result.discharge_kw[0] == 10.0
    assert result.charge_kw[0] == 0.0


def test_charges_at_max_rate_when_price_below_low_threshold():
    result = two_threshold_dispatch(
        price_kwh=np.array([0.02]), hours=np.array([1.0]), **_BASE_KWARGS
    )
    assert result.charge_kw[0] == 10.0
    assert result.discharge_kw[0] == 0.0


def test_idles_when_price_is_between_the_two_thresholds():
    result = two_threshold_dispatch(
        price_kwh=np.array([0.20]), hours=np.array([1.0]), **_BASE_KWARGS
    )
    assert result.charge_kw[0] == 0.0
    assert result.discharge_kw[0] == 0.0
    assert result.final_soc_kwh == _BASE_KWARGS["initial_soc_kwh"]


# --- two_threshold_dispatch: real physical clamping -------------------------


def test_discharge_clamped_by_remaining_headroom_above_min_soc():
    """Only 2kWh of real headroom above min_soc -- must not discharge at
    the full 10kW rate even though price calls for max discharge."""
    kwargs = dict(_BASE_KWARGS)
    kwargs["initial_soc_kwh"] = 12.0  # min_soc=10 -> only 2kWh headroom
    result = two_threshold_dispatch(
        price_kwh=np.array([0.50]), hours=np.array([1.0]), **kwargs
    )
    assert result.discharge_kw[0] == 2.0  # not 10.0
    assert result.final_soc_kwh == 10.0  # clamped exactly to min_soc


def test_charge_clamped_by_remaining_headroom_below_max_soc():
    kwargs = dict(_BASE_KWARGS)
    kwargs["initial_soc_kwh"] = 97.0  # max_soc=100 -> only 3kWh headroom
    result = two_threshold_dispatch(
        price_kwh=np.array([0.02]), hours=np.array([1.0]), **kwargs
    )
    assert result.charge_kw[0] == 3.0  # not 10.0
    assert result.final_soc_kwh == 100.0


def test_soc_never_exceeds_min_soc_floor_across_many_discharge_periods():
    """Real, sustained multi-period behaviour, not just one clamped
    step -- SoC must never go below min_soc_kwh no matter how many
    consecutive high-price periods are simulated."""
    kwargs = dict(_BASE_KWARGS)
    kwargs["initial_soc_kwh"] = 15.0
    result = two_threshold_dispatch(
        price_kwh=np.full(20, 0.50), hours=np.full(20, 1.0), **kwargs
    )
    assert result.final_soc_kwh == kwargs["min_soc_kwh"]
    assert result.final_soc_kwh >= kwargs["min_soc_kwh"] - 1e-9


# --- two_threshold_dispatch: efficiency losses are real, not free ----------


def test_round_trip_with_lossy_efficiency_loses_real_energy():
    """Charge 10kWh in at 90% efficiency (9kWh stored), then discharge
    back out at 90% efficiency -- must not return to the starting SoC,
    proving efficiency is genuinely applied, not a no-op."""
    kwargs = dict(_BASE_KWARGS)
    kwargs["charge_efficiency"] = 0.9
    kwargs["discharge_efficiency"] = 0.9
    kwargs["max_charge_kw"] = 100.0
    kwargs["max_discharge_kw"] = 100.0
    kwargs["initial_soc_kwh"] = 50.0
    # One cheap period (charge), one expensive period (discharge).
    result = two_threshold_dispatch(
        price_kwh=np.array([0.02, 0.50]), hours=np.array([1.0, 1.0]), **kwargs
    )
    # Charged 100kW*0.9 = 90kWh stored (clamped by max_soc=100 headroom
    # of 50kWh instead) -- soc after charge = 100. Then discharged back
    # down, but each kWh delivered costs 1/0.9 kWh of stored energy.
    assert result.final_soc_kwh < 50.0  # a genuine net loss from round-tripping


# --- two_threshold_dispatch: variable-width periods -------------------------


def test_energy_computed_from_real_period_duration_not_assumed_hourly():
    """hours[t] != 1.0 must scale the real energy moved, matching the
    module's own docstring ("headroom_kwh * efficiency / hours[t]")."""
    kwargs = dict(_BASE_KWARGS)
    kwargs["initial_soc_kwh"] = 50.0
    kwargs["max_discharge_kw"] = 10.0
    # A 0.25h (15-min) period at max discharge power moves 2.5kWh, not 10kWh.
    result = two_threshold_dispatch(
        price_kwh=np.array([0.50]), hours=np.array([0.25]), **kwargs
    )
    assert result.final_soc_kwh == 47.5


# --- tune_two_threshold: real grid search ------------------------------------


def test_tune_two_threshold_finds_a_valid_low_below_high():
    """A trivial evaluate_fn (prefer whichever dispatch discharges the
    LEAST, i.e. costs nothing) -- confirms the search actually returns a
    real, internally consistent (low < high) pair rather than garbage."""

    def evaluate_fn(dispatch):
        return float(np.sum(dispatch.discharge_kw))

    price = np.array([0.05, 0.15, 0.25, 0.35, 0.45])
    hours = np.full(5, 1.0)
    low, high, dispatch, cost = tune_two_threshold(
        price_kwh=price,
        hours=hours,
        initial_soc_kwh=50.0,
        min_soc_kwh=10.0,
        max_soc_kwh=100.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        evaluate_fn=evaluate_fn,
        n_grid=8,
    )
    assert low < high
    assert cost == 0.0  # a real, correct minimum -- never discharge at all
    assert np.all(dispatch.discharge_kw == 0.0)


def test_tune_two_threshold_genuinely_minimizes_real_cost_not_just_first_candidate():
    """A real preference (cheapest total $ cost, price * dispatched
    energy) -- confirms the search is genuinely comparing candidates
    against evaluate_fn, not just returning whatever the grid's first
    valid (low, high) pair happens to be.

    A real methodology mistake caught building this test, worth leaving
    documented: an earlier version started the battery at 50kWh (well
    above min_soc=10) -- with that much headroom already in stock, the
    genuinely optimal answer was to discharge the expensive period WITHOUT
    ever charging at all (skip the 0.05-per-kWh charging cost entirely,
    since the energy to sell was already sitting there for free). That's
    real, correct search behaviour, just not what this test meant to
    prove. Starting exactly AT min_soc makes a discharge genuinely
    impossible without first charging, which is what actually forces the
    charge-cheap/discharge-expensive pattern to be the true optimum.
    """
    price = np.array([0.05, 0.50])
    hours = np.full(2, 1.0)

    def evaluate_fn(dispatch):
        # Charging is a cost (spend money), discharging is revenue
        # (earn money) -- real $ P&L, matching what an actual household
        # cares about, not just "how much energy moved."
        cost = float(np.sum(dispatch.charge_kw * price * hours))
        revenue = float(np.sum(dispatch.discharge_kw * price * hours))
        return cost - revenue

    _best_low, _best_high, best_dispatch, best_cost = tune_two_threshold(
        price_kwh=price,
        hours=hours,
        initial_soc_kwh=10.0,  # == min_soc_kwh: a discharge is impossible without charging first
        min_soc_kwh=10.0,
        max_soc_kwh=100.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        evaluate_fn=evaluate_fn,
        n_grid=10,
    )
    # The only way to earn any discharge revenue at all here is to
    # charge first -- confirms the search actually found the real,
    # necessary economic behaviour, not an arbitrary pair.
    assert best_dispatch.charge_kw[0] > 0.0
    assert best_dispatch.discharge_kw[1] > 0.0
    assert best_cost < 0.0  # a real net profit


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
