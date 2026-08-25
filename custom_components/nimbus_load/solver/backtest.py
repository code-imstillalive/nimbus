"""Retrospective parameter-sensitivity backtesting -- "if you'd used a
different setting, knowing what actually happened that day, would you
have come out ahead or behind?" (2026-08-25, direct household ask for
an "outstanding... truly special unique" idea, narrowed to: an offline,
continuous backtesting engine that proves Nimbus's own decisions
against reality, rather than a bigger LP or a fancier model).

## What this genuinely can and cannot test (read before adding a
## candidate parameter)

This module re-solves a real, already-elapsed day's REAL, KNOWN
load/solar/price (the same "perfect foresight" convention regret.py's
own oracle_dispatch() already uses for EPR's own j_star) under an
ALTERNATIVE BatteryConfig/GridConfig, and scores the result the same
way. This is honest and cheap for any parameter that changes the LP's
own economic tradeoff even under PERFECT knowledge -- efficiency,
charge/discharge friction cost, capacity, SoC limits, power limits,
salvage value. These all directly appear in J (see regret.py's own
module docstring) or the feasible region, so a different value
genuinely produces a different re-solved plan and a different score.

It CANNOT meaningfully test `risk_aversion` / `import_price_risk_
aversion` / `export_price_risk_aversion`, or "what if a different
forecast source had been used". Both of those only have any effect
when a genuine forecast CONFIDENCE BAND is present (see network.py's
own `_risk_adjusted()`/`_risk_adjusted_bound()` -- risk_aversion is a
mathematically exact no-op whenever `lower_kw`/`upper_kw` are None) --
and this module's whole "score against real, already-known ground
truth" approach has, by construction, zero uncertainty to hedge
against. Testing risk_aversion this way would silently produce the
EXACT SAME score for every candidate, every single day, which looks
like "risk_aversion doesn't matter" when it actually means "this
measurement method structurally cannot see it." Do not add it here.
A genuine risk_aversion/forecast-source backtest needs Nimbus to
ARCHIVE each day's real forecast + confidence band at the time it was
published -- a real, disclosed, NOT-YET-BUILT prerequisite, not
something to fake by silently reusing this module's own machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .regret import evaluate_realized_cost, oracle_dispatch


@dataclass(frozen=True)
class CandidateResult:
    """One candidate parameter value's real economic outcome for ONE
    real, already-elapsed day -- re-solved and scored against that same
    day's real known load/solar/price."""

    label: str
    total_cost: float


def score_candidate_day(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
) -> float:
    """Re-solves ONE real, already-elapsed day's real known load/solar/
    price under the given battery/grid config, returns the resulting
    total_cost. Literally oracle_dispatch() + evaluate_realized_cost(),
    scored under the SAME candidate config the dispatch was optimized
    for -- consistent with "what would the LP have decided, and how
    would that decision have scored, if this had been the real
    assumption all along."

    Raises whatever oracle_dispatch() raises (a genuinely infeasible
    scenario) -- callers running a sweep across many days/candidates
    must catch this per-candidate, not let one bad combination abort
    the whole sweep (see run_efficiency_sensitivity_sweep()'s own
    docstring).
    """
    charge_kw, discharge_kw, final_soc_kwh = oracle_dispatch(
        periods=periods, grid=grid, battery=battery, solar=solar, load=load
    )
    result = evaluate_realized_cost(
        hours=periods.hours,
        load_real_kw=load.forecast_kw,
        solar_real_kw=solar.forecast_kw,
        import_price_real=grid.import_price,
        export_price_real=grid.export_price,
        charge_committed_kw=charge_kw,
        discharge_committed_kw=discharge_kw,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=final_soc_kwh,
        salvage_value=battery.salvage_value,
        grid_import_limit_kw=grid.import_limit_kw,
        grid_export_limit_kw=grid.export_limit_kw,
    )
    return result.total_cost


# Bounded, curated candidate set (2026-08-25) -- deliberately NOT a
# combinatorial grid across every economic parameter at once. Each
# sweep varies ONE parameter, holding everything else at the
# household's real configured value, so a result is directly
# interpretable ("here's how sensitive your economics are to
# round-trip efficiency specifically") rather than an opaque
# many-dimensional search. Revisit if/when a real household asks for
# a specific OTHER parameter's own sensitivity -- adding one is a
# small, mechanical extension of the same pattern, not a redesign.
EFFICIENCY_CANDIDATES_PERCENT: tuple[float, ...] = (85.0, 90.0, 95.0, 99.0)
"""99.0, not 100.0 -- BatteryConfig's own __post_init__ deliberately
REJECTS exactly 100% efficiency as a real degeneracy guard (see
elements.py's own DegenerateConfigError), independent of this module.
99% is close enough to "near-perfect" for a genuinely useful sensitivity
read while staying strictly inside the valid (0, 1) range."""


def run_efficiency_sensitivity_sweep(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    base_battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
    candidates_percent: tuple[float, ...] = EFFICIENCY_CANDIDATES_PERCENT,
) -> list[CandidateResult]:
    """Re-solves the SAME real day under each candidate round-trip
    efficiency, holding every other battery/grid setting at the
    household's real configured value. Mirrors solver_writer.py's own
    sqrt-split efficiency convention: a candidate expressed as a single
    round-trip percentage is applied as charge_efficiency =
    discharge_efficiency = sqrt(candidate / 100).

    Each candidate is solved independently and defensively -- a single
    candidate's own infeasibility (e.g. an absurdly low efficiency
    making the real day's real load unservable within real power
    limits) is skipped, not allowed to abort the whole sweep, since the
    genuinely useful output is "here's what DID solve," not an
    all-or-nothing batch.
    """
    results: list[CandidateResult] = []
    for pct in candidates_percent:
        efficiency = (pct / 100.0) ** 0.5
        candidate_battery = replace(
            base_battery,
            charge_efficiency=efficiency,
            discharge_efficiency=efficiency,
        )
        try:
            total_cost = score_candidate_day(
                periods=periods,
                grid=grid,
                battery=candidate_battery,
                solar=solar,
                load=load,
            )
        except RuntimeError:
            continue
        results.append(CandidateResult(label=f"{pct:.0f}%", total_cost=total_cost))
    return results
