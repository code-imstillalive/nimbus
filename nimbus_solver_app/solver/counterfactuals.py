"""Three counterfactual controllers, per Mark Purcell's own ask #3:
"Three counterfactuals, not one: no control, a tuned two-threshold price
rule with no forecasting, and the oracle. Report the fraction of the
naive-to-oracle gap NIMBUS actually closes."

Combined with regret.py's own `oracle_dispatch()` (the third
counterfactual already built there) and a real run of `rolling.py`
(what NIMBUS itself achieves), this module is what lets a caller compute
Mark's own requested fraction:

    closed_fraction = (J_no_control - J_nimbus) / (J_no_control - J*)

Both functions here return a real, simulated dispatch trajectory --
never an LP result -- since neither counterfactual is an optimizer:
`no_control_dispatch()` has no decision to make at all (the battery is
physically disabled), and `two_threshold_dispatch()` is a plain reactive
rule using only the REAL, current-moment price and SoC, with zero
forecast of any kind (the whole point of this counterfactual -- it is
what a household with a battery but no software optimization at all
would actually do).

Observation only -- never writes anything anywhere, same as every other
module in this package.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class CounterfactualDispatch:
    """A real, simulated (charge, discharge) trajectory plus the final
    SoC reached -- the same shape regret.py's evaluate_realized_cost()
    already expects for `charge_committed_kw`/`discharge_committed_kw`.
    """

    charge_kw: NDArray[np.float64]
    discharge_kw: NDArray[np.float64]
    final_soc_kwh: float


def no_control_dispatch(
    *, n_periods: int, initial_soc_kwh: float
) -> CounterfactualDispatch:
    """The battery physically disabled -- zero charge, zero discharge,
    every period. No forecast dependency at all (there is no decision
    being made), which is the whole point of this baseline: it is what
    "no battery, or a battery nobody ever dispatches" actually costs.
    """
    zeros = np.zeros(n_periods)
    return CounterfactualDispatch(
        charge_kw=zeros, discharge_kw=zeros, final_soc_kwh=initial_soc_kwh
    )


def two_threshold_dispatch(
    *,
    price_kwh: NDArray[np.float64],
    hours: NDArray[np.float64],
    initial_soc_kwh: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    threshold_low: float,
    threshold_high: float,
) -> CounterfactualDispatch:
    """A plain reactive rule, no forecast, no optimization: discharge at
    max rate when the REAL CURRENT price exceeds `threshold_high`,
    charge at max rate when it's below `threshold_low`, otherwise idle
    -- each period decided using only that period's own real price and
    the battery's own real current SoC (a genuine, simulated forward
    pass, never an LP). This is the real "unsophisticated but not
    stupid" baseline Mark's ask #3 calls for -- a household with a
    battery and a simple, human-settable rule, no software forecasting
    of any kind. Both rates are physically clamped by remaining SoC
    headroom every period, same as a real inverter would be.
    """
    n = len(price_kwh)
    charge = np.zeros(n)
    discharge = np.zeros(n)
    soc = initial_soc_kwh
    for t in range(n):
        if price_kwh[t] > threshold_high:
            headroom_kwh = max(0.0, soc - min_soc_kwh)
            max_by_energy = (
                headroom_kwh * discharge_efficiency / hours[t] if hours[t] > 0 else 0.0
            )
            d = min(max_discharge_kw, max_by_energy)
            discharge[t] = d
            soc -= d * hours[t] / discharge_efficiency
        elif price_kwh[t] < threshold_low:
            headroom_kwh = max(0.0, max_soc_kwh - soc)
            max_by_energy = (
                headroom_kwh / (charge_efficiency * hours[t]) if hours[t] > 0 else 0.0
            )
            c = min(max_charge_kw, max_by_energy)
            charge[t] = c
            soc += c * charge_efficiency * hours[t]
        # else: idle, soc unchanged
    return CounterfactualDispatch(
        charge_kw=charge, discharge_kw=discharge, final_soc_kwh=soc
    )


def tune_two_threshold(
    *,
    price_kwh: NDArray[np.float64],
    hours: NDArray[np.float64],
    initial_soc_kwh: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    evaluate_fn,
    n_grid: int = 15,
) -> tuple[float, float, CounterfactualDispatch, float]:
    """Grid-search the (threshold_low, threshold_high) pair that
    minimizes REAL cost on THIS specific real price window, via
    `evaluate_fn(dispatch) -> float` (the caller's own
    evaluate_realized_cost() closure, so this stays decoupled from
    regret.py's own signature). "Tuned" per Mark's own word -- this is a
    real search over real outcomes, not a guessed pair of numbers.
    Returns (best_low, best_high, best_dispatch, best_cost).
    """
    candidates = np.linspace(float(np.min(price_kwh)), float(np.max(price_kwh)), n_grid)
    best = None
    for low in candidates:
        for high in candidates:
            if high <= low:
                continue
            dispatch = two_threshold_dispatch(
                price_kwh=price_kwh,
                hours=hours,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                charge_efficiency=charge_efficiency,
                discharge_efficiency=discharge_efficiency,
                threshold_low=low,
                threshold_high=high,
            )
            cost = evaluate_fn(dispatch)
            if best is None or cost < best[3]:
                best = (float(low), float(high), dispatch, cost)
    return best
